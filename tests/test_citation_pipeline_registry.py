"""Registry consistency + dispatch tests for meta_agent.citation_pipeline.

The registry is the single source of truth for citation kinds. This file
verifies:
  - Schema's CITATION_KIND Literal stays in lockstep with the registry
    (the assertion in pipelines.schemas fires at module load — re-run here
    in case future code edits accidentally suppress it)
  - The registry has one pipeline per declared kind
  - verify_citation dispatches to the right pipeline by kind
  - Unknown kind fails closed (returns CheckResult(False, ...))
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import get_args

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def test_schema_literal_matches_registry():
    """CITATION_KIND must enumerate exactly the kinds the registry handles —
    otherwise pydantic accepts kinds the verifier can't dispatch (or rejects
    kinds it can)."""
    from meta_agent.citation_pipeline import all_kinds
    from pipelines.schemas import CITATION_KIND
    assert set(get_args(CITATION_KIND)) == set(all_kinds())


def test_registry_has_one_pipeline_per_kind():
    """Every kind in the registry is bound to a unique CitationPipeline."""
    from meta_agent.citation_pipeline import _REGISTRY, CitationPipeline
    seen_classes = set()
    for kind, pipeline in _REGISTRY.items():
        assert isinstance(pipeline, CitationPipeline)
        assert pipeline.kind == kind, f"registry key {kind!r} != pipeline.kind {pipeline.kind!r}"
        cls = type(pipeline)
        assert cls not in seen_classes, f"duplicate pipeline class: {cls.__name__}"
        seen_classes.add(cls)


def test_get_pipeline_returns_correct_kind():
    from meta_agent.citation_pipeline import get_pipeline, all_kinds
    for kind in all_kinds():
        assert get_pipeline(kind).kind == kind


def test_get_pipeline_raises_on_unknown_kind():
    from meta_agent.citation_pipeline import get_pipeline
    with pytest.raises(KeyError, match="unknown citation kind"):
        get_pipeline("invented_kind")


async def test_verify_citation_dispatches_by_kind():
    """verify_citation hands off to the right pipeline. Quick smoke: a
    well-formed citation with a non-existent evidence_id should fail with
    the structural-existence reason from the dispatched pipeline."""
    from meta_agent.citation_pipeline import verify_citation
    res = await verify_citation({
        "kind": "computed_indicator",
        "evidence_id": 999_999_999,
        "source_ref_id": "X",
        "quote": "z",
    })
    assert res.ok is False
    assert "not in evidence_snapshot" in res.reason


async def test_verify_citation_unknown_kind_fails_closed():
    from meta_agent.citation_pipeline import verify_citation
    res = await verify_citation({
        "kind": "not_a_real_kind",
        "evidence_id": 1, "source_ref_id": "x", "quote": "y",
    })
    assert res.ok is False
    assert "unknown citation kind" in res.reason


async def test_verify_citation_handles_missing_kind():
    from meta_agent.citation_pipeline import verify_citation
    res = await verify_citation({
        "evidence_id": 1, "source_ref_id": "x", "quote": "y",
    })
    assert res.ok is False
    assert "missing/non-string kind" in res.reason


async def test_verify_citation_catches_pipeline_crash(monkeypatch):
    """If a pipeline's verify() raises, the registry-level dispatcher should
    convert it to a CheckResult(ok=False) — never let an exception propagate
    to the worker (which would crash the whole verification pass)."""
    from meta_agent import citation_pipeline as cp

    class _BoomPipeline(cp.CitationPipeline):
        kind = "_test_boom"
        async def verify(self, citation):
            raise RuntimeError("intentional test crash")

    # Inject without registering through CITATION_KIND (the schema assertion
    # would fail). Bypass by accessing the private registry directly — tests
    # are allowed.
    monkeypatch.setitem(cp._REGISTRY, "_test_boom", _BoomPipeline())
    res = await cp.verify_citation({"kind": "_test_boom", "evidence_id": 1,
                                     "source_ref_id": "x", "quote": "y"})
    assert res.ok is False
    assert "verifier crashed" in res.reason
