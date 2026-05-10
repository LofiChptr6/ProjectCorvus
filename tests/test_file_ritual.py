"""Phase 4 — file_ritual unit tests.

Each test exercises ONE branch of apply_action: tune happy path, add happy path,
scrap, tune with bad new_content (rollback to backup), add with bad content
(cleanup the new file), MODEL_VERSION mismatch, smoke-test failure rollback,
path-escape rejection.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pipelines import file_ritual


GOOD_MODEL_TEMPLATE = '''"""Test model."""
MODEL_VERSION = "{version}"

def compute(symbol, bars, context):
    return {{
        "direction": "flat",
        "conviction": 0.0,
        "expected_return_pct": 0.0,
        "time_to_target_days": 1,
        "inputs": {{"sym": symbol}},
    }}
'''

BROKEN_SYNTAX = '''MODEL_VERSION = "1.0"
def compute(symbol, bars, context  # missing closing paren
'''

WRONG_OUTPUT_KEYS = '''MODEL_VERSION = "1.0"
def compute(symbol, bars, context):
    return {"direction": "long"}  # missing the other 4 required keys
'''


def _setup_sector_dir(tmp_path: Path, sector: str = "atlas") -> tuple[Path, Path, str]:
    """Make repo_root + sector models dir under a unique top-level package
    (`ritual_test_<uuid>`) so we don't collide with the live `agents/` package
    during import_module. Returns (repo_root, models_root, prefix).
    """
    import uuid
    prefix = f"ritual_test_{uuid.uuid4().hex[:6]}"
    repo_root = tmp_path
    models_root = tmp_path / prefix / sector / "models"
    models_root.mkdir(parents=True)
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    return repo_root, models_root, prefix


def _path(prefix: str, sector: str, *parts: str) -> str:
    return f"{prefix}/{sector}/models/" + "/".join(parts)


# ── tune ──────────────────────────────────────────────────────────────────────


async def test_tune_happy_path(tmp_path):
    repo_root, models_root, prefix = _setup_sector_dir(tmp_path)
    target = models_root / "regime.py"
    target.write_text(GOOD_MODEL_TEMPLATE.format(version="1.0"))

    new = GOOD_MODEL_TEMPLATE.format(version="1.1").replace('"flat"', '"long"')
    result = await file_ritual.apply_action(
        action="tune",
        file_path=_path(prefix, "atlas", "regime.py"),
        new_content=new, new_version="1.1",
        allowed_root=models_root, repo_root=repo_root,
    )
    assert result.ok, f"failed at stage={result.stage}: {result.error}"
    assert result.stage == "complete"
    assert result.backup_path is not None
    assert Path(result.backup_path).exists()
    assert '"long"' in target.read_text()


async def test_tune_failure_rolls_back(tmp_path):
    repo_root, models_root, prefix = _setup_sector_dir(tmp_path)
    target = models_root / "regime2.py"
    original = GOOD_MODEL_TEMPLATE.format(version="1.0")
    target.write_text(original)

    result = await file_ritual.apply_action(
        action="tune",
        file_path=_path(prefix, "atlas", "regime2.py"),
        new_content=BROKEN_SYNTAX, new_version="1.0",
        allowed_root=models_root, repo_root=repo_root,
    )
    assert result.ok is False
    assert result.stage == "import"
    assert result.restored is True
    assert target.read_text() == original


async def test_tune_smoke_test_failure_rolls_back(tmp_path):
    repo_root, models_root, prefix = _setup_sector_dir(tmp_path)
    target = models_root / "regime3.py"
    original = GOOD_MODEL_TEMPLATE.format(version="1.0")
    target.write_text(original)

    result = await file_ritual.apply_action(
        action="tune",
        file_path=_path(prefix, "atlas", "regime3.py"),
        new_content=WRONG_OUTPUT_KEYS, new_version="1.0",
        allowed_root=models_root, repo_root=repo_root,
        smoke_test=lambda mod: file_ritual.default_smoke_test(mod, sample_symbols=["SPY"]),
    )
    assert result.ok is False
    assert result.stage == "smoke"
    assert "missing keys" in (result.error or "")
    assert result.restored is True
    assert target.read_text() == original


async def test_tune_version_mismatch_caught_pre_write(tmp_path):
    repo_root, models_root, prefix = _setup_sector_dir(tmp_path)
    target = models_root / "regime4.py"
    target.write_text(GOOD_MODEL_TEMPLATE.format(version="1.0"))

    new = GOOD_MODEL_TEMPLATE.format(version="1.5")
    result = await file_ritual.apply_action(
        action="tune",
        file_path=_path(prefix, "atlas", "regime4.py"),
        new_content=new, new_version="2.0",
        allowed_root=models_root, repo_root=repo_root,
    )
    assert result.ok is False
    assert result.stage == "version_check"
    assert "MODEL_VERSION mismatch" in (result.error or "")
    assert "1.0" in target.read_text()


# ── add ───────────────────────────────────────────────────────────────────────


async def test_add_happy_path(tmp_path):
    repo_root, models_root, prefix = _setup_sector_dir(tmp_path)
    target = models_root / "fresh.py"
    assert not target.exists()

    new = GOOD_MODEL_TEMPLATE.format(version="1.0")
    result = await file_ritual.apply_action(
        action="add",
        file_path=_path(prefix, "atlas", "fresh.py"),
        new_content=new, new_version="1.0",
        allowed_root=models_root, repo_root=repo_root,
    )
    assert result.ok, f"failed at stage={result.stage}: {result.error}"
    assert target.exists()
    assert result.backup_path is None


async def test_add_existing_file_rejected(tmp_path):
    repo_root, models_root, prefix = _setup_sector_dir(tmp_path)
    target = models_root / "exists.py"
    target.write_text(GOOD_MODEL_TEMPLATE.format(version="1.0"))

    result = await file_ritual.apply_action(
        action="add",
        file_path=_path(prefix, "atlas", "exists.py"),
        new_content=GOOD_MODEL_TEMPLATE.format(version="1.0"), new_version="1.0",
        allowed_root=models_root, repo_root=repo_root,
    )
    assert result.ok is False
    assert "already exists" in (result.error or "")


async def test_add_failure_deletes_new_file(tmp_path):
    repo_root, models_root, prefix = _setup_sector_dir(tmp_path)
    target = models_root / "broken_new.py"
    assert not target.exists()

    result = await file_ritual.apply_action(
        action="add",
        file_path=_path(prefix, "atlas", "broken_new.py"),
        new_content=BROKEN_SYNTAX, new_version="1.0",
        allowed_root=models_root, repo_root=repo_root,
    )
    assert result.ok is False
    assert result.stage == "import"
    assert not target.exists()
    assert result.restored is True


# ── scrap ─────────────────────────────────────────────────────────────────────


async def test_scrap_moves_file_to_scrapped_dir(tmp_path):
    repo_root, models_root, prefix = _setup_sector_dir(tmp_path)
    target = models_root / "old.py"
    target.write_text(GOOD_MODEL_TEMPLATE.format(version="1.0"))

    result = await file_ritual.apply_action(
        action="scrap",
        file_path=_path(prefix, "atlas", "old.py"),
        new_content=None, new_version=None,
        allowed_root=models_root, repo_root=repo_root,
    )
    assert result.ok is True
    assert not target.exists()
    assert result.backup_path is not None
    moved = Path(result.backup_path)
    assert moved.exists()
    assert "scrapped" in moved.parts
    assert moved.name == "old.py"


async def test_scrap_missing_file_errors(tmp_path):
    repo_root, models_root, prefix = _setup_sector_dir(tmp_path)
    result = await file_ritual.apply_action(
        action="scrap",
        file_path=_path(prefix, "atlas", "ghost.py"),
        new_content=None, new_version=None,
        allowed_root=models_root, repo_root=repo_root,
    )
    assert result.ok is False
    assert "does not exist" in (result.error or "")


# ── path-escape defense ───────────────────────────────────────────────────────


def test_safe_target_path_rejects_dotdot_escape(tmp_path):
    repo_root, models_root, prefix = _setup_sector_dir(tmp_path)
    with pytest.raises(ValueError):
        file_ritual.safe_target_path(
            f"{prefix}/atlas/models/../../../etc/passwd",
            allowed_root=models_root, repo_root=repo_root,
        )


def test_safe_target_path_accepts_subdir(tmp_path):
    repo_root, models_root, prefix = _setup_sector_dir(tmp_path)
    p = file_ritual.safe_target_path(
        _path(prefix, "atlas", "sub", "foo.py"),
        allowed_root=models_root, repo_root=repo_root,
    )
    assert "sub" in p.parts
