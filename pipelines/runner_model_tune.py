"""Model-tune write path: takes the validated ModelTuneOutput and runs each
file_action through file_ritual, then writes the hypothesis log and records
the kind="model_change" thesis.

Shadow mode: file actions write to `agents/<sector>/models/.shadow/<filename>`
instead of the real models dir. Useful for cutover validation — the real
auto-discovery loader doesn't see .shadow files."""
from __future__ import annotations

import logging
from datetime import date as _date, timedelta
from pathlib import Path
from typing import Any, Optional

from db import store
from pipelines import file_ritual
from pipelines.schemas import ModelTuneOutput

log = logging.getLogger(__name__)


def _models_root(agent_name: str, dry_run: bool, repo_root: Path) -> Path:
    if dry_run:
        # Outside the live models/ dir so the auto-discovery loader cannot
        # accidentally pick up shadow files. The leading-letter directory
        # name also keeps Python's import machinery happy (a `.shadow`
        # subdir would translate to a malformed module name).
        return repo_root / "agents" / agent_name / "models_shadow"
    return repo_root / "agents" / agent_name / "models"


def _hypothesis_log_path(agent_name: str, dry_run: bool, repo_root: Path) -> Path:
    base = repo_root / "agents" / agent_name / "notes"
    name = "model_hypothesis.shadow.md" if dry_run else "model_hypothesis.md"
    return base / name


async def apply_model_tune_output(
    parsed: ModelTuneOutput,
    *,
    agent_name: str,
    dry_run: bool = False,
    session_id: Optional[str] = None,
    repo_root: Optional[Path] = None,
    smoke_test: Optional[file_ritual.SmokeTestFn] = None,
) -> dict[str, Any]:
    repo_root = repo_root or Path.cwd()
    models_root = _models_root(agent_name, dry_run, repo_root)
    models_root.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "models_root": str(models_root),
        "actions": [],
        "actions_ok": 0,
        "actions_failed": 0,
        "hypothesis_log_path": None,
        "thesis_id": None,
    }

    # Compute the target path under models_root regardless of what prefix the
    # LLM used. We split the LLM's path on "/models/" and treat everything to
    # the right as the relative subpath; that subpath is then re-rooted under
    # models_root (which is `.shadow/` in dry-run mode).
    for action in parsed.file_actions:
        action_path = Path(action.file_path)
        parts = action_path.parts
        if "models" in parts:
            idx = parts.index("models")
            rel_to_models = Path(*parts[idx + 1:])
        else:
            rel_to_models = action_path
        absolute_target = (models_root / rel_to_models).resolve()
        target_path = str(absolute_target.relative_to(repo_root.resolve()))

        result = await file_ritual.apply_action(
            action=action.action,
            file_path=target_path,
            new_content=action.new_content,
            new_version=action.new_version,
            allowed_root=models_root,
            repo_root=repo_root,
            smoke_test=smoke_test,
        )
        summary["actions"].append({
            "action": action.action,
            "file_path": target_path,
            "ok": result.ok,
            "stage": result.stage,
            "error": result.error,
            "restored": result.restored,
            "backup_path": result.backup_path,
            "reason": action.reason,
        })
        if result.ok:
            summary["actions_ok"] += 1
        else:
            summary["actions_failed"] += 1

    # Write hypothesis log update.
    hyp_path = _hypothesis_log_path(agent_name, dry_run, repo_root)
    hyp_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        hyp_path.write_text(parsed.hypothesis_log_update, encoding="utf-8")
        summary["hypothesis_log_path"] = str(hyp_path)
    except Exception as e:
        log.warning("hypothesis_log write failed: %s", e)

    # Record the model_change thesis to the live journal even in dry-run —
    # the thesis IS the record of what was proposed; live/shadow only differs
    # on whether the files actually moved on disk.
    try:
        verify_by = parsed.thesis.verify_by or (_date.today() + timedelta(days=7)).isoformat()
        thesis_id = await store.record_thesis(
            agent_name=agent_name,
            kind=parsed.thesis.kind,
            title=parsed.thesis.title,
            body=parsed.thesis.body,
            verify_by=verify_by,
            parent_id=parsed.thesis.parent_id,
            market_snapshot=parsed.thesis.market_snapshot,
        )
        summary["thesis_id"] = thesis_id
    except Exception as e:
        log.warning("record_thesis failed: %s", e)

    # Telegram fires in BOTH modes; dry-run prepends [DRY-RUN] to the
    # message so the user can validate the tune-cycle summary contract end-
    # to-end without confusion. File mutations stayed in models_shadow/ —
    # only the tune-cycle audit/UX surfaces fire for real.
    from pipelines import notify
    summary["telegram_sent"] = await notify.send_summary_safe(
        agent_name, parsed.telegram_summary, dry_run=dry_run,
    )

    return summary
