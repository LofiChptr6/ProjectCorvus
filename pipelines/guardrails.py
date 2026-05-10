"""Per-skill-type tool allowlist.

Replaces the `.md` frontmatter `allowed-tools` section of the harness path.
The tool_loop only declares the schemas in this allowlist to vLLM, so the
LLM cannot call tools outside its lane even if it tries — `dispatch` would
return `{"error": "unknown tool"}` because the schema isn't registered with
the model.
"""
from __future__ import annotations

# Skill-type → set of tool names the LLM may call during tool-loop.
AGENT_TOOL_ALLOWLIST: dict[str, set[str]] = {
    "respond": {
        "get_quote",
        "compute_technicals",
        "get_bars",
        "get_news",
        "get_my_active_views",
        "get_my_journal",
        "mark_inbox_responded",
    },
    "review": {
        # Read-only investigation tools. Writes happen via structured-output JSON,
        # NOT via tool dispatch — ConvictionView / ForecastRow / ThesisRecord
        # are batch-written by the orchestrator after pydantic validation.
        "get_quote",
        "get_bars",
        "compute_technicals",
        "compute_all_models",
        "get_news",
        "get_my_active_views",
        "get_my_journal",
    },
    "evening": set(),  # single-shot, no tool loop — bundle is sufficient
    "model_tune": {
        # Read-only investigation. File mutations come through structured output
        # (ModelFileAction list), NOT via tool dispatch — orchestrator owns the
        # backup/write/import-check/smoke-test ritual.
        "get_quote",
        "get_bars",
        "compute_technicals",
        "compute_all_models",
        "get_news",
        "get_my_active_views",
        "get_my_journal",
    },
}


# Default knobs per skill type — tool-loop iteration cap and token budget.
# review/evening/model_tune emit structured-output JSON, so the budget must
# accommodate Qwen3 thinking-block prelude AND the full JSON payload. Earlier
# 4096 was too tight: Qwen3 ate the whole budget on `<think>` and never
# emitted the JSON. 8192 gives breathing room.
SKILL_LIMITS: dict[str, dict[str, int]] = {
    "respond": {"max_iter": 4, "max_tokens": 1500},
    "review":  {"max_iter": 8, "max_tokens": 8192},
    "evening": {"max_iter": 1, "max_tokens": 8192},  # single-shot, no tool loop
    "model_tune": {"max_iter": 6, "max_tokens": 12000},
}


# Skills whose final assistant message must be parseable JSON. For these,
# the runner asks vLLM to skip Qwen3 thinking-mode prelude — there's no value
# in chain-of-thought when the deterministic schema covers the reasoning surface,
# and thinking blocks bloat tokens + risk truncating the actual JSON.
DISABLE_THINKING_SKILLS: set[str] = {"review", "evening", "model_tune"}


def allowlist_for(skill_type: str) -> set[str]:
    if skill_type not in AGENT_TOOL_ALLOWLIST:
        raise KeyError(f"no allowlist for skill_type={skill_type!r}")
    return AGENT_TOOL_ALLOWLIST[skill_type]


def limits_for(skill_type: str) -> dict[str, int]:
    return SKILL_LIMITS.get(skill_type, {"max_iter": 6, "max_tokens": 2048})


# Dev-mode prefix mirrors scripts/run_scheduled_skill.sh:18-22 — when set, the
# template should drop STEP-0 skip-fast guards and label outbound Telegrams
# with [DEV]. Pipelines pass `dev_mode=True` to the template renderer to
# inject this.
DEV_MODE_PREFIX = (
    "DEV-MODE: For this run only, SKIP all STEP 0 skip-fast guards: "
    "market_closed, quiet_window, kill_switch, and was_open checks. Run "
    "the full review using whatever stale data is available so the user "
    "can see your thinking. Prefix every Telegram message with [DEV] so "
    "it is not confused with live signal. Do NOT place any real orders -- "
    "analysis only."
)
