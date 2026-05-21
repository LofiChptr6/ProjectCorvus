"""Citation kind registry — single source of truth for what kinds of evidence
a `Citation` can pin to, and how each kind is verified.

Before this module, citation kinds were duplicated in three places:
  - pipelines/schemas.py   (CITATION_KIND Literal)
  - db/store.py            (_EVIDENCE_VALID_KINDS set)
  - scripts/run_verify_worker.py  (_VALID_CITATION_KINDS + _check_X functions)

Adding a new kind required 5+ edits across those files. This module collapses
the surface to one place. Each kind is a subclass of `CitationPipeline` and
gets auto-registered; `pipelines.schemas` asserts that its Literal type stays
in sync.

To add a new citation kind:
  1. Subclass `CitationPipeline` below, implement `verify()`.
  2. Add an instance to `_REGISTRY`.
  3. Add the kind name to `pipelines.schemas.CITATION_KIND` Literal.
     (Module-load assertion fails loudly if you forget.)
"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class CheckResult:
    """Per-citation verification result.

    ok=True  → citation is in good standing
    ok=False → citation flagged with `reason`

    `replay_ok` is the optional secondary signal: True when we successfully
    re-executed the evidence-producing tool and got matching output; False
    when re-execution diverged; None when this kind doesn't support replay.
    """
    ok: bool
    reason: Optional[str] = None
    replay_ok: Optional[bool] = None


# ── Shared helpers ──────────────────────────────────────────────────────────


async def _resolve_snapshot(
    citation: dict, expected_kind: str,
) -> tuple[Optional[dict], Optional[CheckResult]]:
    """Look up evidence_snapshot[citation.evidence_id], confirm kind +
    source_ref_id match. Returns (snapshot, None) on success or
    (None, failing CheckResult) on any structural problem.

    Pipelines that ground their evidence in evidence_snapshot (4 of 5 kinds)
    call this as the first step in verify(). ModelRunPipeline opts out — it
    grounds in agent_forecast directly.
    """
    from db import store

    ev_id = citation.get("evidence_id")
    if not isinstance(ev_id, int) or ev_id < 1:
        return None, CheckResult(False, f"evidence_id invalid: {ev_id!r}")
    snap = await store.get_evidence_snapshot(ev_id)
    if snap is None:
        return None, CheckResult(False, f"evidence_id={ev_id} not in evidence_snapshot")
    if snap["kind"] != expected_kind:
        return None, CheckResult(
            False,
            f"kind mismatch: citation says {expected_kind!r}, evidence row is {snap['kind']!r}",
        )
    src = citation.get("source_ref_id")
    if src and snap["source_ref_id"] != src:
        return None, CheckResult(
            False,
            f"source_ref_id mismatch: citation={src!r}, evidence={snap['source_ref_id']!r}",
        )
    return snap, None


# ── Base class + concrete pipelines ─────────────────────────────────────────


class CitationPipeline(abc.ABC):
    """Pipeline for one citation kind: structural check + (optional) replay.

    Subclasses must set `kind` (the string used in `Citation.kind`) and
    implement `verify(citation) -> CheckResult`.

    Subclasses MUST NOT call `verify()` on each other — verification is
    per-citation, isolated by design (CoVe principle). Cross-pipeline
    composition belongs in a higher layer if ever needed.
    """
    kind: str

    @abc.abstractmethod
    async def verify(self, citation: dict) -> CheckResult:
        ...


class NewsPostPipeline(CitationPipeline):
    """`kind='news_post'` — evidence is a search-result snapshot from
    query_news or verify_catalyst. Structural check only; no replay (re-running
    the same news query against today's feed could legitimately produce a
    different match set as new news arrives)."""
    kind = "news_post"

    async def verify(self, citation: dict) -> CheckResult:
        _, fail = await _resolve_snapshot(citation, self.kind)
        if fail is not None:
            return fail
        return CheckResult(True)


class ModelRunPipeline(CitationPipeline):
    """`kind='model_run'` — evidence is a forecast_run_id (UUID) in
    agent_forecast. This kind is the one exception to "every citation points
    at an evidence_snapshot row": model_run citations use evidence_id=0 as
    a sentinel and resolve via agent_forecast directly.

    The verification check is existence of the forecast_run_id."""
    kind = "model_run"

    async def verify(self, citation: dict) -> CheckResult:
        run_id = citation.get("source_ref_id")
        if not run_id:
            return CheckResult(False, "model_run citation missing source_ref_id (forecast_run_id)")
        from db.schema import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM agent_forecast WHERE forecast_run_id = $1::uuid LIMIT 1",
                str(run_id),
            )
        if row is None:
            return CheckResult(False, f"forecast_run_id={run_id} not in agent_forecast", replay_ok=False)
        return CheckResult(True, replay_ok=True)


class ComputedIndicatorPipeline(CitationPipeline):
    """`kind='computed_indicator'` — evidence is a deterministic indicator
    computation. We re-run the same tool with the snapshot's inputs and
    compare the result against the snapshot's outputs. A mismatch (value
    drifted) is a soft flag — the snapshot still reflects what was true at
    decision time."""
    kind = "computed_indicator"

    async def verify(self, citation: dict) -> CheckResult:
        snap, fail = await _resolve_snapshot(citation, self.kind)
        if fail is not None:
            return fail
        inputs = snap.get("inputs_json") or {}
        sym = inputs.get("symbol")
        indicator = inputs.get("indicator")
        asof = inputs.get("asof")
        if not (sym and indicator):
            return CheckResult(False, f"snapshot inputs missing symbol/indicator: {inputs}")
        from tools.analysis.compute_indicator import execute
        res = await execute(symbol=sym, indicator=indicator, asof=asof)
        if not res.get("ok"):
            return CheckResult(False, f"replay declined: {res.get('reason')}", replay_ok=False)
        snap_out = snap.get("outputs_json") or {}
        if res.get("value") != snap_out.get("value"):
            return CheckResult(
                False,
                f"replay value drifted: snapshot={snap_out.get('value')!r} fresh={res.get('value')!r}",
                replay_ok=False,
            )
        return CheckResult(True, replay_ok=True)


class PriorThesisPipeline(CitationPipeline):
    """`kind='prior_thesis'` — evidence_snapshot row with source_ref_id
    pointing at agent_thesis.id. Structural check + existence check on the
    referenced thesis row (catches chains broken by thesis deletion)."""
    kind = "prior_thesis"

    async def verify(self, citation: dict) -> CheckResult:
        snap, fail = await _resolve_snapshot(citation, self.kind)
        if fail is not None:
            return fail
        # Source_ref_id should resolve to a real thesis id
        src = snap["source_ref_id"]
        try:
            thesis_id = int(src)
        except (TypeError, ValueError):
            return CheckResult(False, f"prior_thesis source_ref_id not numeric: {src!r}")
        from db.schema import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM agent_thesis WHERE id = $1 LIMIT 1", thesis_id,
            )
        if row is None:
            return CheckResult(False, f"thesis_id={thesis_id} no longer exists")
        return CheckResult(True)


class SiblingViewPipeline(CitationPipeline):
    """`kind='sibling_view'` — another agent's active conviction.
    evidence_snapshot points at agent_conviction.id; we check the row still
    exists AND is not expired."""
    kind = "sibling_view"

    async def verify(self, citation: dict) -> CheckResult:
        snap, fail = await _resolve_snapshot(citation, self.kind)
        if fail is not None:
            return fail
        src = snap["source_ref_id"]
        try:
            conv_id = int(src)
        except (TypeError, ValueError):
            return CheckResult(False, f"sibling_view source_ref_id not numeric: {src!r}")
        from db.schema import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT expires_at < NOW() AS expired
                   FROM agent_conviction WHERE id = $1 LIMIT 1""",
                conv_id,
            )
        if row is None:
            return CheckResult(False, f"sibling conviction id={conv_id} not found")
        if row["expired"]:
            return CheckResult(False, f"sibling conviction id={conv_id} has expired")
        return CheckResult(True)


# ── Registry ────────────────────────────────────────────────────────────────


_REGISTRY: dict[str, CitationPipeline] = {
    p.kind: p for p in (
        NewsPostPipeline(),
        ModelRunPipeline(),
        ComputedIndicatorPipeline(),
        PriorThesisPipeline(),
        SiblingViewPipeline(),
    )
}


def all_kinds() -> frozenset[str]:
    """All citation kinds the harness knows how to verify. Single source
    of truth — `pipelines.schemas.CITATION_KIND` and `db.store` defer to
    this set."""
    return frozenset(_REGISTRY.keys())


def get_pipeline(kind: str) -> CitationPipeline:
    """Look up the pipeline for a citation kind. Raises KeyError on
    unknown kinds — caller should treat that as an automatic verification
    failure."""
    try:
        return _REGISTRY[kind]
    except KeyError:
        raise KeyError(
            f"unknown citation kind: {kind!r} (registered: {sorted(_REGISTRY)})"
        )


async def verify_citation(citation: dict) -> CheckResult:
    """Single dispatch entry-point for the worker. Looks up the pipeline by
    kind, calls verify(), returns the result. Unknown kind → automatic fail."""
    kind = citation.get("kind")
    if not isinstance(kind, str):
        return CheckResult(False, f"citation missing/non-string kind: {kind!r}")
    try:
        pipeline = get_pipeline(kind)
    except KeyError as exc:
        return CheckResult(False, str(exc))
    try:
        return await pipeline.verify(citation)
    except Exception as exc:
        log.exception("citation_pipeline.%s crashed: %s", kind, exc)
        return CheckResult(False, f"verifier crashed: {type(exc).__name__}: {exc}")
