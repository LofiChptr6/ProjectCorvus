"""Top-level pipeline runner.

`run_skill(agent_name, skill_type)` is the single entrypoint replacing
`scripts/run_scheduled_skill.sh` for migrated skill types.

Two execution shapes:
  - **respond** — tool-loop only. Writes happen via the `mark_inbox_responded`
    tool dispatch inside the loop.
  - **review / evening / model_tune** — tool-loop (read-only investigation) +
    structured-output validation (final assistant message must be JSON
    matching the skill's pydantic schema). The orchestrator validates and
    batch-writes via `db.store` functions on success. On parse/validation
    failure: one retry with the error appended to history; second failure
    skips the write phase, logs loudly.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pydantic
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from pipelines import guardrails, llm_client, schemas, tool_dispatch, tool_loop

log = logging.getLogger(__name__)


_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "agent" / "templates"


def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
    )


@dataclass
class SkillResult:
    agent_name: str
    skill_type: str
    session_id: str
    final_text: str
    iterations: int
    finish_reason: str
    duration_ms: int
    tool_call_log: list[dict[str, Any]] = field(default_factory=list)
    skipped: bool = False
    skip_reason: Optional[str] = None
    parsed_output: Optional[dict[str, Any]] = None
    write_summary: Optional[dict[str, Any]] = None
    validation_errors: list[str] = field(default_factory=list)


# ── Bundler dispatch ─────────────────────────────────────────────────────────


async def _build_bundle(agent_name: str, skill_type: str) -> Any:
    if skill_type == "respond":
        from agent.bundlers.respond import get_respond_bundle
        return await get_respond_bundle(agent_name)
    if skill_type == "review":
        from agent.bundlers.review import get_review_bundle
        return await get_review_bundle(agent_name)
    if skill_type == "evening":
        from agent.bundlers.evening import get_evening_bundle
        return await get_evening_bundle(agent_name)
    if skill_type == "model_tune":
        from agent.bundlers.model_tune import get_model_tune_bundle
        return await get_model_tune_bundle(agent_name)
    raise ValueError(f"unknown skill_type={skill_type!r}")


def _render_template(skill_type: str, *, agent_name: str, bundle: Any, dev_mode: bool) -> str:
    env = _jinja_env()
    tpl = env.get_template(f"{skill_type}.j2")
    return tpl.render(
        agent_name=agent_name,
        bundle=bundle,
        dev_mode=dev_mode,
        dev_prefix=guardrails.DEV_MODE_PREFIX if dev_mode else "",
    )


def _build_system_prompt(agent_name: str) -> str:
    try:
        from agent.agent_registry import load_agent
        from agent.prompt_builder import build_system_prompt
        agent_cfg = load_agent(agent_name)
        return build_system_prompt(agent_cfg, {})
    except Exception as exc:
        log.warning("build_system_prompt fallback: %s", exc)
        return f"You are {agent_name}, a sector analyst on a multi-agent quant trading desk."


# ── Structured-output validation ─────────────────────────────────────────────


_SCHEMA_FOR_SKILL: dict[str, type[pydantic.BaseModel]] = {
    "review": schemas.ReviewOutput,
    "evening": schemas.EveningOutput,
    "model_tune": schemas.ModelTuneOutput,
}


import re as _re

_THINK_RE = _re.compile(r"<think>.*?</think>", _re.DOTALL)


def _strip_code_fence(s: str) -> str:
    """Normalize an LLM response into raw JSON.

    Handles three stylistic wrappings the model emits even when told otherwise:
      - Qwen3 `<think>...</think>` reasoning blocks (stripped)
      - markdown ```json ... ``` fences (stripped)
      - prose before/after a JSON object (we extract the outermost { ... } if
        the trimmed string isn't already JSON-shaped)
    """
    s = _THINK_RE.sub("", s).strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[: -3]
        s = s.strip()
    # If still not JSON-shaped, try to grab the first {...} balanced block.
    if s and s[0] != "{":
        first = s.find("{")
        last = s.rfind("}")
        if first != -1 and last != -1 and last > first:
            s = s[first : last + 1]
    return s.strip()


def _parse_structured(skill_type: str, text: str) -> tuple[Optional[pydantic.BaseModel], Optional[str]]:
    model = _SCHEMA_FOR_SKILL.get(skill_type)
    if model is None:
        return None, f"no schema registered for skill_type={skill_type!r}"
    try:
        payload = json.loads(_strip_code_fence(text))
    except json.JSONDecodeError as e:
        return None, f"json decode: {e}"
    try:
        return model.model_validate(payload), None
    except pydantic.ValidationError as e:
        return None, f"validation: {e}"


# ── Write dispatch (post-validation) ─────────────────────────────────────────


async def _resolve_conviction_fields(
    c: schemas.ConvictionView,
    agent_name: str,
) -> Optional[dict[str, Any]]:
    """Return resolved numeric fields for a conviction row, or None to drop.

    When `c.from_model` is set, run the helper and override LLM-authored
    direction/conviction/expected_return_pct/time_to_target_days/stop_pct/
    model_inputs with the model's output. Helper returns skipped/error → drop
    with a log line. Model-emitted "short" → drop (inverse-ETF routing
    convention: direct shorts are allocator-inert; the LLM should express
    bearishness via an inverse-ETF long instead).

    When `from_model` is None, pass the LLM-authored fields through unchanged.
    """
    if not c.from_model:
        return {
            "direction": c.direction,
            "conviction": c.conviction,
            "expected_return_pct": c.expected_return_pct,
            "time_to_target_days": c.time_to_target_days,
            "stop_pct": c.stop_pct,
            "model_inputs": c.model_inputs,
        }
    from meta_agent.conviction_from_model import compute_conviction_payload
    res = await compute_conviction_payload(agent_name, c.from_model, c.symbol)
    if res["status"] == "skipped":
        log.info("review.from_model skipped: agent=%s sym=%s model=%s reason=%s",
                 agent_name, c.symbol, c.from_model, res["reason"])
        return None
    if res["status"] == "error":
        log.warning("review.from_model error: agent=%s sym=%s model=%s err=%s",
                    agent_name, c.symbol, c.from_model, res["error"])
        return None
    p = res["payload"]
    if p["direction"] == "short":
        log.info("review.from_model dropped short: agent=%s sym=%s model=%s "
                 "(allocator skips direct shorts; LLM should express via inverse-ETF long)",
                 agent_name, c.symbol, c.from_model)
        return None
    return {
        "direction": p["direction"],
        "conviction": p["conviction"],
        "expected_return_pct": p["expected_return_pct"],
        "time_to_target_days": p["time_to_target_days"],
        "stop_pct": p["stop_pct"],
        "model_inputs": p["model_inputs"],  # already stamped with _model/_version
    }


async def _apply_review_output(
    parsed: schemas.ReviewOutput,
    *,
    agent_name: str,
    dry_run: bool,
    session_id: str,
) -> dict[str, Any]:
    from db import store
    summary: dict[str, Any] = {
        "convictions_inserted": 0,
        "forecasts_inserted": 0,
        "forecast_errors": [],
        "theses_recorded": 0,
        "theses_graded": 0,
        "dry_run": dry_run,
    }

    # Trading-impacting writes (convictions/forecasts) — routed to *_shadow
    # tables in dry-run, live tables otherwise. This is the ONE thing dry-run
    # actually changes; everything else (theses, Telegram) fires either way
    # so the pipeline gets exercised end-to-end. Dry-run telegrams are
    # tagged `[DRY-RUN]` so the user can distinguish at a glance.
    if dry_run:
        await store.clear_agent_convictions_shadow(agent_name)
        for c in parsed.convictions:
            resolved = await _resolve_conviction_fields(c, agent_name)
            if resolved is None:
                continue
            await store.insert_conviction_shadow(
                agent_name=agent_name,
                symbol=c.symbol,
                direction=resolved["direction"],
                conviction=resolved["conviction"],
                expected_return_pct=resolved["expected_return_pct"],
                time_to_target_days=resolved["time_to_target_days"],
                rationale=c.rationale,
                model_inputs=resolved["model_inputs"],
                expires_in_hours=c.expires_in_hours,
                momentum_confirmed=c.momentum_confirmed,
                stop_pct=resolved["stop_pct"],
                run_session_id=session_id,
            )
            summary["convictions_inserted"] += 1

        await store.clear_agent_forecasts_shadow(agent_name)
        result = await store.insert_forecasts_batch_shadow(
            agent_name,
            [f.model_dump() for f in parsed.forecasts],
            run_session_id=session_id,
        )
        summary["forecasts_inserted"] = result["inserted"]
        summary["forecast_errors"] = result["errors"]
    else:
        # Snapshot prior (direction, first_held_since) so we can preserve the
        # entry anchor across the clear+upsert cycle. Without this snapshot,
        # every review would reset first_held_since because clear deletes the
        # row before upsert recreates it. The preservation rule fires only when
        # the agent re-publishes the same direction on the same symbol — flips
        # (long↔flat, long↔short) correctly reset the anchor to NOW().
        prior_rows = await store.get_agent_active_convictions(agent_name)
        prior_anchor: dict[str, tuple[str, object]] = {
            r["symbol"]: (r["direction"], r.get("first_held_since"))
            for r in prior_rows
        }
        await store.clear_agent_convictions(agent_name)
        for c in parsed.convictions:
            resolved = await _resolve_conviction_fields(c, agent_name)
            if resolved is None:
                continue
            prev = prior_anchor.get(c.symbol.upper())
            preserved = prev[1] if (prev and prev[0] == resolved["direction"]) else None
            await store.upsert_conviction(
                agent_name=agent_name,
                symbol=c.symbol,
                direction=resolved["direction"],
                conviction=resolved["conviction"],
                expected_return_pct=resolved["expected_return_pct"],
                time_to_target_days=resolved["time_to_target_days"],
                rationale=c.rationale,
                model_inputs=resolved["model_inputs"],
                expires_in_hours=c.expires_in_hours,
                momentum_confirmed=c.momentum_confirmed,
                stop_pct=resolved["stop_pct"],
                first_held_since=preserved,
            )
            summary["convictions_inserted"] += 1

        await store.clear_agent_forecasts(agent_name)
        result = await store.upsert_forecasts_batch(
            agent_name, [f.model_dump() for f in parsed.forecasts],
        )
        summary["forecasts_inserted"] = result.get("inserted", 0)
        summary["forecast_errors"] = result.get("errors", [])

    # Theses (journal) — write in BOTH modes. Dry-run validates the full
    # pipeline; theses are append-only audit, low blast radius.
    for t in parsed.theses_to_record:
        await store.record_thesis(
            agent_name=agent_name,
            kind=t.kind, title=t.title, body=t.body,
            verify_by=t.verify_by, parent_id=t.parent_id,
            market_snapshot=t.market_snapshot,
            primary_symbol=t.primary_symbol,
            direction=t.direction,
            entry_price=t.entry_price,
        )
        summary["theses_recorded"] += 1

    for g in parsed.theses_to_grade:
        await store.update_thesis_status(
            thesis_id=g.thesis_id, status=g.status,
            resolution_note=g.resolution_note, agent_name=agent_name,
        )
        summary["theses_graded"] += 1

    # Telegram — fires in BOTH modes. Dry-run prepends `[DRY-RUN] ` so the
    # user can validate the full per-sector message contract before flipping.
    from pipelines import notify
    summary["telegram_sent"] = await notify.send_summary_safe(
        agent_name, parsed.telegram_summary, dry_run=dry_run,
    )

    return summary


# ── Top-level entrypoint ─────────────────────────────────────────────────────


async def run_skill(
    agent_name: str,
    skill_type: str,
    *,
    dev_mode: bool = False,
    dry_run: bool = False,
    session_id: Optional[str] = None,
) -> SkillResult:
    started = time.time()
    bundle = await _build_bundle(agent_name, skill_type)

    # Skip-fast for respond: empty inbox → exit silently.
    if skill_type == "respond" and not getattr(bundle, "pending_inbox", []):
        return SkillResult(
            agent_name=agent_name,
            skill_type=skill_type,
            session_id=session_id or "",
            final_text="",
            iterations=0,
            finish_reason="empty_inbox",
            duration_ms=int((time.time() - started) * 1000),
            skipped=True,
            skip_reason="empty_inbox",
        )

    system = _build_system_prompt(agent_name)
    user = _render_template(skill_type, agent_name=agent_name, bundle=bundle, dev_mode=dev_mode)

    skill_name = f"{agent_name}-{skill_type.replace('_', '-')}"
    cli = llm_client.make_client(skill_name, session_id=session_id)
    allowlist = guardrails.allowlist_for(skill_type)
    schema_list = tool_dispatch.filter_schemas(allowlist) if allowlist else []
    limits = guardrails.limits_for(skill_type)

    result = await tool_loop.run(
        client=cli.client,
        model=cli.model,
        system=system,
        user=user,
        tool_schemas=schema_list,
        dispatch_fn=tool_dispatch.dispatch,
        max_iter=limits["max_iter"],
        max_tokens=limits["max_tokens"],
        disable_thinking=(skill_type in guardrails.DISABLE_THINKING_SKILLS),
    )

    parsed_obj = None
    parsed_dict = None
    write_summary = None
    validation_errors: list[str] = []

    if skill_type in _SCHEMA_FOR_SKILL:
        parsed_obj, err = _parse_structured(skill_type, result.final_text)
        if err is not None and parsed_obj is None:
            validation_errors.append(err)
            log.warning(
                "structured-output validation failed (%s); first 400 chars: %r",
                err, (result.final_text or "")[:400],
            )
            # Retry once: append the error, re-run a single non-tool turn.
            retry = await _retry_structured_output(
                cli=cli, system=system, history=result.history,
                error=err, max_tokens=limits["max_tokens"],
            )
            if retry is not None:
                result.final_text = retry
                parsed_obj, err2 = _parse_structured(skill_type, retry)
                if err2 is not None:
                    validation_errors.append(err2)
                    log.warning(
                        "retry also failed (%s); first 400 chars: %r",
                        err2, (retry or "")[:400],
                    )

        if parsed_obj is not None:
            parsed_dict = parsed_obj.model_dump()
            if skill_type == "review":
                write_summary = await _apply_review_output(
                    parsed_obj, agent_name=agent_name,
                    dry_run=dry_run, session_id=cli.session_id,
                )
            elif skill_type == "evening":
                from pipelines.runner_evening import apply_evening_output
                write_summary = await apply_evening_output(
                    parsed_obj, agent_name=agent_name, dry_run=dry_run,
                )
            elif skill_type == "model_tune":
                from pipelines.runner_model_tune import apply_model_tune_output
                write_summary = await apply_model_tune_output(
                    parsed_obj, agent_name=agent_name,
                    dry_run=dry_run, session_id=cli.session_id,
                )

    return SkillResult(
        agent_name=agent_name,
        skill_type=skill_type,
        session_id=cli.session_id,
        final_text=result.final_text,
        iterations=result.iterations,
        finish_reason=result.finish_reason,
        duration_ms=int((time.time() - started) * 1000),
        tool_call_log=result.tool_call_log,
        parsed_output=parsed_dict,
        write_summary=write_summary,
        validation_errors=validation_errors,
    )


async def _retry_structured_output(
    *,
    cli: llm_client.LLMClient,
    system: str,
    history: list[dict[str, Any]],
    error: str,
    max_tokens: int,
) -> Optional[str]:
    """One-shot retry: append a structured error message and ask for valid JSON."""
    retry_messages = list(history) + [{
        "role": "user",
        "content": (
            f"Your previous final message did not parse as the required JSON schema. "
            f"Error: {error}\n\nReturn ONLY valid JSON conforming to the schema in your "
            f"prior instructions. No prose wrapper, no markdown fences."
        ),
    }]
    try:
        resp = await cli.client.chat.completions.create(
            model=cli.model,
            max_tokens=max_tokens,
            temperature=0.0,
            messages=retry_messages,
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        log.warning("retry_structured_output failed: %s", e)
        return None
