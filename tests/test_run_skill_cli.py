"""CLI smoke test for scripts/run_skill.py.

Exercises the empty-inbox short-circuit path so we don't need a live LLM.
Confirms the wrapper does the right thing: parses args, sets up logging,
emits a JSON status line on stdout, exits 0 on skip.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


async def test_cli_empty_inbox_exits_clean(test_agent):
    """No pending inbox row → CLI exits 0 with skipped=True."""
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
    }
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/run_skill.py"), test_agent, "respond"],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=15,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"

    # stdout should have one JSON line.
    line = result.stdout.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["agent"] == test_agent
    assert payload["skill"] == "respond"
    assert payload["skipped"] is True
    assert payload["skip_reason"] == "empty_inbox"


def test_cli_bad_skill_type_rejected():
    """argparse should reject unknown skill_type."""
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/run_skill.py"), "atlas", "not_a_skill"],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=10,
    )
    assert result.returncode == 2  # argparse error code
    assert "invalid choice" in result.stderr.lower()


async def test_cli_dry_run_flag_propagates(test_agent):
    """--dry-run is reflected in the JSON status line + log message."""
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/run_skill.py"),
         test_agent, "respond", "--dry-run"],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=15,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    line = result.stdout.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["dry_run"] is True


async def test_cli_pipeline_dry_run_env_var_propagates(test_agent):
    """PIPELINE_DRY_RUN env var enables dry-run when --dry-run flag is omitted."""
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PIPELINE_DRY_RUN": "1"}
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/run_skill.py"),
         test_agent, "respond"],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=15,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    line = result.stdout.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["dry_run"] is True
