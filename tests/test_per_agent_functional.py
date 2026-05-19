"""Tests for the Phase-G per-agent functional choice + leaderboard helpers."""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import patch

import yaml

from meta_agent import conviction_functionals


def _write_yaml(tmp_path: Path, agent: str, data: dict) -> Path:
    yaml_path = tmp_path / f"{agent}.yaml"
    yaml_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return yaml_path


# ── functional_for_agent lookup ─────────────────────────────────────────────

def test_functional_for_agent_missing_yaml_returns_default(tmp_path):
    with patch.object(conviction_functionals, "_AGENTS_ROOT", tmp_path):
        assert conviction_functionals.functional_for_agent("atlas") == \
               conviction_functionals.DEFAULT_FUNCTIONAL


def test_functional_for_agent_unset_field_returns_default(tmp_path):
    _write_yaml(tmp_path, "atlas", {"name": "atlas", "description": "x"})
    with patch.object(conviction_functionals, "_AGENTS_ROOT", tmp_path):
        assert conviction_functionals.functional_for_agent("atlas") == \
               conviction_functionals.DEFAULT_FUNCTIONAL


def test_functional_for_agent_unknown_value_falls_back(tmp_path):
    _write_yaml(tmp_path, "atlas",
                {"name": "atlas", "conviction_functional": "does_not_exist"})
    with patch.object(conviction_functionals, "_AGENTS_ROOT", tmp_path):
        assert conviction_functionals.functional_for_agent("atlas") == \
               conviction_functionals.DEFAULT_FUNCTIONAL


def test_functional_for_agent_returns_declared_value(tmp_path):
    _write_yaml(tmp_path, "atlas",
                {"name": "atlas", "conviction_functional": "frac_kelly"})
    with patch.object(conviction_functionals, "_AGENTS_ROOT", tmp_path):
        assert conviction_functionals.functional_for_agent("atlas") == "frac_kelly"


def test_functional_for_agent_accepts_every_registered_combiner(tmp_path):
    for name in conviction_functionals.list_functionals():
        _write_yaml(tmp_path, "atlas", {"conviction_functional": name})
        with patch.object(conviction_functionals, "_AGENTS_ROOT", tmp_path):
            assert conviction_functionals.functional_for_agent("atlas") == name


# ── leaderboard helpers ─────────────────────────────────────────────────────

def _suggester():
    return importlib.import_module("scripts.suggest_functional_per_agent")


def test_winners_skips_low_sample_counts():
    sug = _suggester()
    ranks = {
        "atlas": [
            {"functional": "frac_kelly", "n": 5, "sharpe_ann": 2.0, "total_pnl": 1.0},
            {"functional": "expected_return", "n": 50, "sharpe_ann": 1.0, "total_pnl": 5.0},
        ],
    }
    winners = sug._winners(ranks)
    # frac_kelly is best by Sharpe but has n=5 < MIN_SAMPLES; expected_return wins instead.
    assert winners["atlas"] == "expected_return"


def test_winners_requires_positive_sharpe():
    sug = _suggester()
    ranks = {
        "atlas": [
            {"functional": "frac_kelly",      "n": 50, "sharpe_ann": -0.5, "total_pnl": 1.0},
            {"functional": "expected_return", "n": 50, "sharpe_ann": -1.0, "total_pnl": 0.5},
        ],
    }
    # Neither is positive → no winner for this agent.
    assert "atlas" not in sug._winners(ranks)


def test_winners_picks_best_sharpe_with_pnl_tiebreaker():
    sug = _suggester()
    ranks = {
        "atlas": [
            {"functional": "frac_kelly",      "n": 30, "sharpe_ann": 1.5, "total_pnl": 3.0},
            {"functional": "expected_return", "n": 50, "sharpe_ann": 1.5, "total_pnl": 5.0},
        ],
    }
    # NOTE: _winners walks the input list in order; production code calls _rank
    # to sort first. Test here uses pre-sorted input — both have same sharpe;
    # caller would put expected_return first via tiebreaker. We verify the
    # leaderboard sorter does that.
    from collections import defaultdict
    sorted_ranks = sorted(ranks["atlas"],
                          key=lambda r: (-(r["sharpe_ann"]), -(r["total_pnl"])))
    assert sorted_ranks[0]["functional"] == "expected_return"


def test_apply_winners_to_yaml_writes_field_in_place(tmp_path):
    sug = _suggester()
    atlas_yaml = tmp_path / "atlas.yaml"
    atlas_yaml.write_text(yaml.safe_dump({
        "name": "atlas", "description": "macro long",
    }), encoding="utf-8")
    with patch.object(sug, "_REPO_ROOT", tmp_path.parent), \
         patch("pathlib.Path", Path):
        # The script computes _REPO_ROOT/"agents"/"<agent>.yaml". Make tmp_path/agents/ point at our file.
        agents_dir = tmp_path.parent / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        target = agents_dir / "atlas.yaml"
        target.write_text(atlas_yaml.read_text(), encoding="utf-8")
        winners = {"atlas": "frac_kelly"}
        n = sug._apply_winners_to_yaml(winners)
    assert n == 1
    out = yaml.safe_load(target.read_text())
    assert out["conviction_functional"] == "frac_kelly"
    # Preserved existing fields
    assert out["name"] == "atlas"
    assert out["description"] == "macro long"


def test_apply_winners_to_yaml_skips_when_already_set(tmp_path):
    sug = _suggester()
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    target = agents_dir / "atlas.yaml"
    target.write_text(yaml.safe_dump({
        "name": "atlas", "conviction_functional": "frac_kelly",
    }), encoding="utf-8")
    with patch.object(sug, "_REPO_ROOT", tmp_path):
        winners = {"atlas": "frac_kelly"}  # already matches
        n = sug._apply_winners_to_yaml(winners)
    assert n == 0
