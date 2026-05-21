# CITATION_ARCH — Open Work (Next Sessions)

Companion to `CITATION_ARCH.md`. Phases A/B/C and the abstract-pipeline
refactor shipped 2026-05-21. This file tracks what's queued next, in
order of leverage.

---

## 1. Phase D — make citations mandatory for `direction='long'`

**Goal**: flip the schema so non-flat convictions without citations get
rejected at parse time. Currently citations are optional (Phase A soft
migration); verifier passes empty-citation rows with a "soft migration"
note.

**Implementation outline**
- `pipelines/schemas.ConvictionView` — add a `model_validator(mode="after")`
  that rejects `direction="long"` + empty citations.
- Gate behind an env knob `CITATION_REQUIRE_MODE` (default `"warn"` for
  staged rollout; flip to `"reject"` after 7 days of observation). Pattern
  matches `MODEL_INPUTS_VALIDATOR_MODE`.
- Update `agents/CITATION_ARCH.md` §3 / §7 Phase D to reflect the actual
  flip date and any agent-prompt nudges added.
- Worker: remove the "Phase A soft-migration" note path; empty citations
  now mean the conviction never reached the worker.

**Prereqs**
- Observe ≥7 days of post-launch behavior. Need real data on what
  fraction of LLM-emitted long convictions arrive with non-empty
  citations. If the rate is <70%, tighten the prompt template first
  before flipping the schema.

**Cost**: ~50 lines + a one-line env var. Mostly the env-knob plumbing
and the validation message tuning.

**Risk**: high. A premature flip will cause every existing long
conviction to fail validation and the agents will publish only flat
rows until the prompt teaches them to attach citations. Stage carefully.

---

## 2. Haiku semantic check (CoVe stage 3)

**Goal**: the verifier worker today does structural + deterministic
replay checks. The third tier from the CoVe literature is "is the
citation's quote semantically supported by the evidence content?" —
catches the case where the LLM cites a real evidence_id but uses a
quote that misrepresents what the evidence says.

**Implementation outline**
- New method on `CitationPipeline` base class: `semantic_check(citation,
  snapshot) -> CheckResult`. Default impl returns
  `CheckResult(True, replay_ok=None)` (no-op). Each pipeline opts in.
- Per-pipeline implementations call Haiku via the existing LLM proxy
  with a prompt like:
  ```
  Citation quote: "{citation.quote}"
  Evidence content: "{snapshot.content_snippet}"
  Is the quote supported by the evidence? yes | no | partial | unclear
  ```
- Worker `_aggregate_action` extends to consider both structural and
  semantic results: any "no" → reject; any "partial" → downgrade; all
  "yes"/"unclear" → pass.
- Gate behind `CITATION_SEMANTIC_CHECK_MODE` env knob; default `"off"`
  to start.

**Prereqs**
- Phase D not strictly required, but more useful once citations are
  mandatory.
- A cheap-model endpoint for Haiku. Check `LLM_PROXY_URL` config for
  multi-model support; may need a separate env var
  (`CITATION_VERIFIER_LLM_MODEL=claude-haiku-4-5`).

**Cost**: ~150 lines for the per-pipeline impls + the dispatch + the
prompt. Plus per-call LLM cost (~$0.00005 per citation × ~50
citations/hour × 24 hours = ~$0.06/day at current volume).

**Risk**: low to medium. The check runs in a separate worker process
post-write; LLM unavailability degrades gracefully to "no semantic
check applied" — never blocks the trading path.

---

## 3. Kill the `model_run` `evidence_id=0` sentinel

**Goal**: the `ModelRunPipeline` is the one pipeline that doesn't ground
in `evidence_snapshot`. Citations of kind `model_run` carry
`evidence_id=0` as a sentinel and the pipeline joins to
`agent_forecast` instead. The wart is contained inside the pipeline
class (since the refactor), but the asymmetry breaks the "every
citation has a real evidence row" invariant.

**Implementation outline**
- In `meta_agent.conviction_from_model._persist_distributions`: after
  writing rows to `agent_forecast`, also stamp an `evidence_snapshot`
  row with `kind="model_run"`, `source_ref_id=forecast_run_id`,
  `outputs_json={direction, expected_return_pct, likelihood,
  time_to_target_days, conviction}`, `computed_by=f"model:{model_name}@{model_version}"`.
- Update `pipelines/runner._resolve_conviction_fields` auto-citation
  minting to use the real `evidence_id` instead of `0`.
- Update `ModelRunPipeline.verify` to call `_resolve_snapshot` like
  every other pipeline, then add the agent_forecast existence check
  on top.
- Backward compat: keep the `evidence_id=0` exemption around for
  ~14 days to grade existing rows; then remove.

**Prereqs**: none.

**Cost**: ~40 lines. Pure cleanup, no behavior change for graded
verifications.

**Risk**: low. Existing model_run citations continue to verify the
same way during transition; new ones gain a real evidence row.

---

## 4. Allocator integration

**Goal**: the verifier writes `action ∈ {pass, downgrade, reject}` per
conviction but the allocator doesn't read it yet. Hook the size
calculation so:
- `pass` → full size
- `downgrade` → size × 0.5 (or some haircut)
- `reject` → size = 0
- No verification row yet → full size (graceful degradation; the
  worker is async)

**Implementation outline**
- `meta_agent.allocator.compute_conviction`: optional `agent_conviction_id`
  param. If passed, look up `latest_verification(id)` and apply haircut.
- Wire in `mike-allocator` / wherever allocator sizing is computed.
- Knob: `ALLOCATOR_VERIFICATION_HAIRCUTS = {"pass": 1.0, "downgrade":
  0.5, "reject": 0.0}` — env-configurable for tuning.

**Prereqs**
- Phase 2 (semantic check) is more meaningful BEFORE this lands —
  otherwise haircuts only fire on structural fabrications, which are
  already rare post-Phase-A. Sequence them: semantic check first,
  then allocator gating.

**Cost**: ~30 lines once the helper exists. Plus careful tuning of
the haircut magnitudes once 30+ days of operating data accumulate.

**Risk**: medium. Pulling sizing on a real conviction because the
verifier called it `reject` is impactful. Stage behind a feature flag
(`ALLOCATOR_USE_VERIFICATION=false` by default) until comfortable.

---

## 5. Dashboard tile: per-agent verification pass rate

**Goal**: simple visibility in `obs/dashboard.py`.

```sql
SELECT c.agent_name,
       COUNT(*) AS n,
       AVG(CASE WHEN v.action='pass' THEN 1.0 ELSE 0.0 END) AS pass_rate,
       AVG(CASE WHEN v.action='downgrade' THEN 1.0 ELSE 0.0 END) AS downgrade_rate,
       AVG(CASE WHEN v.action='reject' THEN 1.0 ELSE 0.0 END) AS reject_rate
FROM agent_conviction c
JOIN LATERAL (
  SELECT action FROM conviction_verification
  WHERE conviction_id = c.id ORDER BY verified_at DESC LIMIT 1
) v ON TRUE
WHERE c.submitted_at >= NOW() - INTERVAL '7 days'
GROUP BY c.agent_name;
```

**Cost**: ~50 lines for the panel.

---

## Order of operations (suggested)

1. **Watch live for ~7 days.** Operating data informs everything below.
2. If post-prompt-update citation rate is healthy: **Phase D in `warn`
   mode** (1).
3. **Kill the model_run sentinel** (3) — pure cleanup, no risk.
4. **Haiku semantic check** (2) — biggest hallucination-catching gain.
5. **Phase D flip to `reject` mode** — by now agents have 14+ days
   to adapt.
6. **Allocator integration** (4) — gate sizing on the now-meaningful
   verification action.
7. **Dashboard tile** (5) — anytime.

---

*Created 2026-05-21 after Phase C + abstract-pipeline refactor shipped.
Update this file as items land. Cross-reference: `CITATION_ARCH.md`
(the canonical spec), `MEMORY.md` (project memory index).*
