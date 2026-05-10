"""Deterministic write ritual for `*-model-tune` skills.

Replaces the LLM-orchestrated dance the `.md` template currently scripts. The
LLM emits a list of `ModelFileAction`s; this module runs each one through:

  backup → write → MODEL_VERSION check → import check → smoke test
                                          ↓ on any failure
                                    restore from backup

For `add` actions (new file, no prior content), failure → delete the file.
For `scrap` actions, the file moves into a `scrapped/<date>/` subdir; the
auto-discovery loader at `agents/<sector>/models/__init__.py` ignores subdirs
so the file stops being invoked without delete.

The smoke test invokes `compute(symbol, bars, context)` on each sample symbol
and verifies the return dict carries the standard keys.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)


REQUIRED_OUTPUT_KEYS = {"direction", "conviction", "expected_return_pct",
                        "time_to_target_days", "inputs"}
_MODEL_VERSION_RE = re.compile(r'^\s*MODEL_VERSION\s*=\s*["\']([^"\']+)["\']\s*$', re.MULTILINE)


@dataclass
class FileRitualResult:
    action: str
    file_path: str
    ok: bool
    stage: str  # last stage attempted ("write" / "import" / "smoke" / "complete" / "rollback")
    error: Optional[str] = None
    backup_path: Optional[str] = None
    restored: bool = False
    new_version: Optional[str] = None


# ── helpers ───────────────────────────────────────────────────────────────────


def _path_to_module(file_path: Path, repo_root: Path) -> str:
    rel = file_path.resolve().relative_to(repo_root.resolve())
    parts = list(rel.with_suffix("").parts)
    return ".".join(parts)


def _check_model_version(content: str, expected: Optional[str]) -> Optional[str]:
    if expected is None:
        return None
    m = _MODEL_VERSION_RE.search(content)
    if not m:
        return f"MODEL_VERSION constant missing (expected {expected!r})"
    if m.group(1) != expected:
        return f"MODEL_VERSION mismatch: file has {m.group(1)!r}, action declared {expected!r}"
    return None


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return await value
    return value


# ── default smoke-test ────────────────────────────────────────────────────────


async def default_smoke_test(
    module_name: str,
    sample_symbols: list[str] | None = None,
    *,
    bars: list[dict] | None = None,
    context: dict[str, Any] | None = None,
) -> Optional[str]:
    """Import `module_name` (reload if cached), call `compute()` on each
    sample symbol, validate output dict shape. Returns error string or None.
    """
    samples = sample_symbols or ["SPY", "TLT", "GLD"]
    bars = bars if bars is not None else []
    context = context if context is not None else {}
    try:
        if module_name in sys.modules:
            module = importlib.reload(sys.modules[module_name])
        else:
            module = importlib.import_module(module_name)
    except Exception as e:
        return f"import: {type(e).__name__}: {e}"

    if not hasattr(module, "compute"):
        return "module missing compute() function"

    for sym in samples:
        try:
            out = await _maybe_await(module.compute(sym, bars, context))
        except Exception as e:
            return f"compute({sym!r}): {type(e).__name__}: {e}"
        if not isinstance(out, dict):
            return f"compute({sym!r}) did not return dict (got {type(out).__name__})"
        missing = REQUIRED_OUTPUT_KEYS - set(out.keys())
        if missing:
            return f"compute({sym!r}) missing keys: {sorted(missing)}"
    return None


SmokeTestFn = Callable[[str], Awaitable[Optional[str]]]


# ── public API ────────────────────────────────────────────────────────────────


def safe_target_path(
    file_path: str,
    *,
    allowed_root: Path,
    repo_root: Optional[Path] = None,
) -> Path:
    """Resolve `file_path` and assert it's inside `allowed_root`.

    `file_path` is the relative path the LLM emitted in its action (e.g.
    'agents/atlas/models/regime_score.py'). `repo_root` is the directory
    `file_path` is relative to (defaults to allowed_root.parent.parent.parent
    for the production layout `<repo>/agents/<sector>/models`). `allowed_root`
    is the sector's models directory; we reject any path that escapes it.
    """
    base = repo_root if repo_root is not None else allowed_root.parent.parent.parent
    candidate = (base / file_path).resolve()
    root = allowed_root.resolve()
    if not str(candidate).startswith(str(root)):
        raise ValueError(f"file_path {file_path!r} escapes allowed_root {allowed_root!r}")
    return candidate


async def apply_action(
    *,
    action: str,
    file_path: str,
    new_content: Optional[str],
    new_version: Optional[str],
    allowed_root: Path,
    repo_root: Path,
    smoke_test: Optional[SmokeTestFn] = None,
) -> FileRitualResult:
    """Apply ONE ModelFileAction with rollback-on-failure semantics.

    `smoke_test` defaults to None which means 'skip smoke testing' (used in
    tests that don't want to run live model code). Production callers pass
    `default_smoke_test` (or a partial) to enable real validation.
    """
    target = safe_target_path(file_path, allowed_root=allowed_root, repo_root=repo_root)
    result = FileRitualResult(action=action, file_path=file_path, ok=False, stage="resolve",
                              new_version=new_version)

    if action == "scrap":
        if not target.is_file():
            result.error = "scrap target does not exist"
            return result
        scrap_dir = target.parent / "scrapped" / datetime.now().strftime("%Y%m%d")
        scrap_dir.mkdir(parents=True, exist_ok=True)
        moved = scrap_dir / target.name
        shutil.move(str(target), str(moved))
        result.ok = True
        result.stage = "complete"
        result.backup_path = str(moved)
        return result

    # tune / add: must have content
    if action not in {"tune", "add"}:
        result.error = f"unknown action: {action!r}"
        return result
    if new_content is None:
        result.error = f"action={action!r} requires new_content"
        return result

    pre_existed = target.is_file()
    if action == "tune" and not pre_existed:
        result.error = "tune target does not exist (use action=add)"
        return result
    if action == "add" and pre_existed:
        result.error = "add target already exists (use action=tune)"
        return result

    # 1. Backup if pre-existing.
    backup: Optional[Path] = None
    if pre_existed:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = target.with_name(target.name + f".bak.{ts}")
        shutil.copy2(target, backup)
        result.backup_path = str(backup)

    # 2. MODEL_VERSION check on the new content.
    err = _check_model_version(new_content, new_version)
    if err:
        result.stage = "version_check"
        result.error = err
        return result

    # 3. Write the new content.
    result.stage = "write"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text(new_content, encoding="utf-8")
    except Exception as e:
        result.error = f"write: {type(e).__name__}: {e}"
        return result

    # 4. Import check.
    result.stage = "import"
    module_name = _path_to_module(target, repo_root)
    try:
        if module_name in sys.modules:
            importlib.reload(sys.modules[module_name])
        else:
            importlib.import_module(module_name)
    except Exception as e:
        result.error = f"import: {type(e).__name__}: {e}"
        await _rollback(action, target, backup, result)
        return result

    # 5. Smoke test (optional).
    if smoke_test is not None:
        result.stage = "smoke"
        smoke_err = await smoke_test(module_name)
        if smoke_err:
            result.error = smoke_err
            await _rollback(action, target, backup, result)
            return result

    result.ok = True
    result.stage = "complete"
    return result


async def _rollback(action: str, target: Path, backup: Optional[Path], result: FileRitualResult) -> None:
    """Restore from backup (tune) or delete the new file (add)."""
    try:
        if action == "tune" and backup is not None:
            shutil.copy2(backup, target)
        elif action == "add":
            target.unlink(missing_ok=True)
        result.restored = True
    except Exception as e:
        log.exception("rollback failed")
        result.error = (result.error or "") + f"; rollback also failed: {e}"
