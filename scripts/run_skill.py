#!/usr/bin/env python3
"""CLI entrypoint for the Python-driven sector skill pipeline.

Usage:
    python scripts/run_skill.py <agent> <skill_type> [--dev]

Replaces `bash scripts/run_scheduled_skill.sh <skill>` for migrated skill
types. Each invocation is one (agent, skill) pair — the orchestrator scripts
fan out concurrently across all 11 sectors via xargs (same pattern as today).

Exit codes:
    0  success (or skipped due to empty inbox / quiet window)
    1  pipeline error
    2  bad CLI args
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# Make `pipelines`, `agent`, `db`, etc. importable when invoked from anywhere.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Load .env so TELEGRAM_BOT_TOKEN, MASSIVE_API_KEY, PG_*, etc. match what
# mcp_server.py / run_scheduled_skill.sh see. The harness path loads via
# mcp_server.py -> dotenv; the pipeline path needs it explicit. Use
# find_dotenv() so worktree runs find the parent repo's .env (which is
# .gitignored and therefore absent from worktrees).
try:
    from dotenv import find_dotenv, load_dotenv
    found = find_dotenv(usecwd=True)
    if found:
        load_dotenv(found)
except Exception:
    pass


def _setup_logging(skill_name: str) -> Path:
    log_dir = _REPO_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"{skill_name}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, mode="a", encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )
    return log_path


async def _amain(agent: str, skill_type: str, dev_mode: bool, dry_run: bool) -> int:
    from pipelines.runner import run_skill
    skill_name = f"{agent}-{skill_type.replace('_', '-')}"
    _setup_logging(skill_name)
    log = logging.getLogger("run_skill")
    log.info("starting agent=%s skill=%s dev=%s dry_run=%s", agent, skill_type, dev_mode, dry_run)
    try:
        result = await run_skill(agent, skill_type, dev_mode=dev_mode, dry_run=dry_run)
    except Exception as exc:
        log.exception("pipeline failed")
        sys.stderr.write(f"FAIL: {type(exc).__name__}: {exc}\n")
        return 1
    log.info(
        "done skill=%s session=%s iters=%d finish=%s duration_ms=%d skipped=%s dry_run=%s",
        skill_name, result.session_id, result.iterations,
        result.finish_reason, result.duration_ms, result.skipped, dry_run,
    )
    sys.stdout.write(json.dumps({
        "agent": agent, "skill": skill_type, "session_id": result.session_id,
        "iterations": result.iterations, "finish": result.finish_reason,
        "duration_ms": result.duration_ms, "skipped": result.skipped,
        "skip_reason": result.skip_reason, "dry_run": dry_run,
        "validation_errors": result.validation_errors,
        "write_summary": result.write_summary,
    }, default=str) + "\n")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Run one sector-skill pipeline turn.")
    p.add_argument("agent", help="Sector agent name (atlas, rex, fab, ...).")
    p.add_argument("skill_type", choices=["respond", "review", "evening", "model_tune"])
    p.add_argument("--dev", action="store_true", help="Inject DEV-mode prefix; no real orders.")
    p.add_argument(
        "--dry-run", action="store_true", default=None,
        help=(
            "Route writes to *_shadow tables / models_shadow/ dir instead of "
            "live tables. For respond, dry-run has no effect (the sole write is "
            "mark_inbox_responded which is always live)."
        ),
    )
    args = p.parse_args()

    # Env-var fallback for orchestrator scripts that find it easier to set
    # WEEKLY_TUNE_DRY_RUN=1 than thread a flag through xargs.
    dry_run = args.dry_run
    if dry_run is None:
        env_flag = os.environ.get("PIPELINE_DRY_RUN") or os.environ.get("WEEKLY_TUNE_DRY_RUN")
        dry_run = bool(env_flag and env_flag != "0" and env_flag.lower() != "false")
    return asyncio.run(_amain(args.agent, args.skill_type, args.dev, dry_run))


if __name__ == "__main__":
    sys.exit(main())
