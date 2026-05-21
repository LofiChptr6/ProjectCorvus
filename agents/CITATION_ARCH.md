# Citation-Grounded Agent Architecture (Design Doc)

Companion to `MODEL_CONTRACT.md`. This document specifies the architecture that
moves the desk from "LLM authors free-form claims" to "LLM proposes intent, the
harness executes and stamps evidence." The migration is Phase-based; this doc is
the target-state spec and the rollout plan.

---

## 1. Why this exists

The 2026-05-21 audit found that every non-flat conviction with non-empty
`model_inputs` in the prior 48h carried fabricated indicator keys (`rsi_14`,
`bbands_upper`, `rolling_std_breach`, …) that no quant model emits. The follow-up
audit traced these to two structural gaps:

- The LLM owns load-bearing numbers and prose in the review pipeline.
- "Verification" happens (if at all) in the same context that produced the claim,
  so the model rationalizes its own output.

The literature converges on three throughlines for fixing this — **enforce
citation, isolate verification, reward refusal** — and on one architectural
pattern that delivers them: **shift work from the model to the harness**. The
Claude Code analysis paper measured a 1.6% / 98.4% split between LLM decision
logic and deterministic infrastructure; today's desk runs closer to 40% / 60%
and the hallucination rate reflects that.

This doc specifies the design that closes the gap.

---

## 2. The throughline → architecture mapping

| Throughline | Architectural lever |
|---|---|
| Every load-bearing claim has a verifiable source | `Citation` typed schema + `evidence_snapshot` append-only table |
| Verification is independent of generation | `verify_worker` consumes new convictions in a fresh LLM context |
| Refusal is a calibration target | `direction='flat'` is allowed with no citations; `'long'` without citations is rejected |
| Move work from model to harness | New `compute_indicator`, `query_news`, `run_skill`, `verify_catalyst` tools — LLM emits intent, harness executes |

---

## 3. Train-of-thought → Structural execution mapping

The core design principle: **for every cognitive step the LLM does today, there
should be a tool that does it deterministically and returns evidence**. The LLM
emits intent (tool_use); the harness returns ground truth (tool_result). Numbers
in context only as tool outputs, never as LLM-authored prose.

| What the LLM wants to know | Tool it calls | Returns |
|---|---|---|
| "Is X overbought right now?" | `compute_indicator(symbol="SCO", indicator="rsi_14", asof=...)` | `{value: 58.3, evidence_id: <uuid>, bars_hash: ...}` |
| "What does the HMM say about Y?" | `run_skill("hmm_regime_mix", symbol="Y")` (thin shim over from_model) | `{distribution, forecast_run_id, evidence_id}` |
| "Is there news on Z this week?" | `query_news(symbol="Z", window="7d")` | `{matches: [post_id, …], evidence_id}` |
| "Did event E happen on date D?" | `verify_catalyst(event="OPEC+ meeting", date="2026-06-04", window_days=2)` | `{found: bool, matching_posts: […], evidence_id}` |
| "Is my prior thesis still valid?" | `run_falsification(thesis_id=T)` | `{falsified: bool, falsification_inputs: …, evidence_id}` |
| "What did atlas say about this sector?" | `read_sibling_view(agent="atlas", symbol_in=[…])` | `{views: [...], evidence_id}` |
| "Has this pattern happened before?" | `semantic_news_recall(query=...)` (exists) | `{matches, evidence_id}` |
| "What's the actual close on SCO today?" | `get_quote(symbol="SCO")` (exists) | `{last, bid, ask, evidence_id}` |
| "What did I conclude last week?" | `read_my_journal(date_range=...)` (exists, gets evidence_id wrapper) | `{rows, evidence_id}` |
| "Cross-asset: how is XLV moving vs XLE?" | `compute_correlation(["XLV","XLE"], window_days=30)` | `{correlation, evidence_id}` |

Every tool returns an `evidence_id` pointing to an `evidence_snapshot` row. The
LLM, when authoring its conviction, attaches `Citation(evidence_id=...)` to each
load-bearing claim. The schema rejects non-flat convictions with empty
citations.

### Design rules for new tools

1. **Determinism**: same inputs + same data state → identical output. No
   wall-clock branching (use `asof` param), no `random` without seed.
2. **Evidence stamping**: every tool result includes an `evidence_id`. The
   harness inserts the snapshot before returning.
3. **Replayable**: each evidence row carries enough metadata
   (`bars_hash`, `news_post_ids`, code path, input args) to re-execute and
   verify the cached result later.
4. **Cheap**: tools target <500ms (skill execution can be slower if dispatched
   via the agent_job queue). Most are SQL queries or vectorized numpy.
5. **No hidden LLM calls inside tools.** If a tool needs interpretation, it
   returns raw data and lets the calling LLM interpret. Keeps the trust chain
   linear.

---

## 4. Schemas

### 4.1 Citation

```python
class Citation(BaseModel):
    kind: Literal["news_post", "model_run", "computed_indicator",
                  "prior_thesis", "sibling_view"]
    ref_id: str               # post.id, forecast_run_id, indicator_run_id, thesis.id, view_id
    quote: str = Field(max_length=300)   # literal text from the evidence
    evidence_id: int          # FK into evidence_snapshot
    extracted_at: datetime    # when the citation was sealed

    @model_validator
    def _resolve_must_exist(self):
        # Verify ref_id resolves to a real row in the corresponding table.
        # Verify evidence_snapshot[evidence_id].kind == self.kind.
        # Verify content_hash on evidence still matches current source
        # (or accept stale flag if outside TTL).
```

### 4.2 Evidence snapshot

```sql
CREATE TABLE evidence_snapshot (
  id              BIGSERIAL PRIMARY KEY,
  kind            TEXT NOT NULL,
  source_ref_id   TEXT NOT NULL,           -- post.id, run_id, thesis.id, etc.
  content_hash    TEXT NOT NULL,           -- SHA256 of canonical evidence
  content_snippet TEXT,                    -- ≤2KB literal evidence
  inputs_json     JSONB,                   -- tool inputs (for replay)
  outputs_json    JSONB,                   -- tool outputs (for replay)
  computed_by     TEXT NOT NULL,           -- tool name + version
  computed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  agent_name      TEXT,                    -- requesting agent (for audit)
  session_id      TEXT,                    -- review session that produced this
  UNIQUE (kind, source_ref_id, content_hash)
);

CREATE INDEX idx_evidence_kind_ref ON evidence_snapshot(kind, source_ref_id);
CREATE INDEX idx_evidence_agent_session ON evidence_snapshot(agent_name, session_id);
```

Two semantics flow from this table:

- **Immutability**: a citation pinned to `evidence_id=N` can always recover
  what the agent saw at decision time. Even if the underlying news article
  is edited or the model is retuned, the snapshot is frozen.
- **Replay**: the verification worker can re-run the tool from `inputs_json`,
  re-hash the output, and assert the cited evidence is still derivable. If
  inputs no longer reproduce the cited output, the citation is flagged.

### 4.3 Rationale

```python
class Rationale(BaseModel):
    summary: str = Field(max_length=600)   # 1-3 sentences prose
    citations: list[Citation]

    # Validators
    # - direction='flat' allows empty citations
    # - direction='long' requires len(citations) >= 1
    # - any indicator term in summary (RSI, BBAND, MACD, …) must have a
    #   computed_indicator citation; otherwise reject (mechanizes H1 from the
    #   plan doc)
```

The free-text `summary` carries the human-readable gloss; the structured
`citations` list is what the resolver and dashboard cross-link.

### 4.4 Thesis with falsification

```python
class Thesis(BaseModel):
    kind: Literal["hypothesis", "prediction", "observation", "opinion"]
    title: str
    body: str
    verify_by: Optional[date]
    catalyst: Optional[Citation]                # required when verify_by set
    falsification: Optional[FalsificationSpec]  # required for kind in {hypothesis, prediction}

class FalsificationSpec(BaseModel):
    skill: str                  # name of a registered skill
    args: dict[str, Any]        # args to pass to the skill
    falsified_when: str         # tiny DSL: "result.value > 0.01"
```

A thesis without a `FalsificationSpec` is `kind="opinion"` and earns no
allocator credit. A nightly resolver runs each thesis's `falsification.skill`
with `args`; if `falsified_when` evaluates true, status flips to `wrong`.
Forces hypotheses to be operationally testable.

---

## 5. The harness (mirrors Claude Code's `tool_use → tool_result` loop)

```
                              Agent review prompt
                                       │
                                       ▼
                              ┌────────────────┐
              ┌──────────────►│  LLM proposes  │
              │               │  tool_use      │  (intent only — no numbers)
              │               └────────┬───────┘
              │                        ▼
              │               ┌────────────────┐
              │               │   Harness:     │  (pipelines/tool_dispatch.py
              │               │   - validate   │   already exists; extend with
              │               │   - execute    │   evidence stamping)
              │               │   - stamp      │
              │               │     evidence   │
              │               └────────┬───────┘
              │                        ▼
              │               ┌────────────────┐
              │               │  tool_result   │  (with evidence_id baked in)
              └───────────────│  + evidence_id │
                              └────────────────┘
                                       │
                                       ▼ (iteration until convergence)
                              ┌────────────────┐
                              │  LLM emits     │
                              │  final JSON    │  (ConvictionView w/ Citation)
                              │  with citations│
                              └────────┬───────┘
                                       ▼
                          schemas.ConvictionView validation
                                       │
                          ┌────────────┴────────────┐
                          ▼                         ▼
                  pipelines/runner.py:        agent_job queue:
                  upsert conviction      ─►   verify_conviction
                                              (separate worker)
```

This is the LATCH split applied to trading reviews: **semantic layer = LLM
deciding what to claim and what to ask; execution layer = deterministic tools
returning ground truth.** Numbers never originate in the LLM — they originate
in tool calls.

---

## 6. Verification worker (CoVe in isolation)

`scripts/run_verify_worker.py` — consumes `verify_conviction` jobs from the
agent_job queue. **It must use a fresh LLM context** (no shared state with the
authoring review) so it cannot rationalize the original claim. Pattern:

```python
# Per conviction row submitted in the last hour:
for citation in conviction.rationale.citations:
    # 1. Re-fetch evidence from source via the same tool that originally ran
    fresh = await rerun_tool(citation.kind, citation.evidence.inputs_json)
    # 2. Compare against the snapshot
    if hash(fresh) != citation.evidence.content_hash:
        flag(conviction, citation, reason="evidence drift")
    # 3. CoVe-style semantic check via a cheap LLM (Haiku):
    prompt = f"Claim: {citation.quote}. Source text: {fresh.text}. Is the claim supported? yes|no|partial"
    if cheap_llm(prompt) == "no":
        flag(conviction, citation, reason="claim not supported by cited source")

# Aggregate: if any flag → downgrade conviction or reject outright
```

The verifier worker does NOT see the original review prose, summary, or sibling
citations. Each citation is checked in isolation against its claimed source.
Mirrors CoVe's "Execute as a double-blind study" insight.

Output: a `conviction_verification` table with one row per checked conviction:

```sql
CREATE TABLE conviction_verification (
  conviction_id   BIGINT NOT NULL,
  verified_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  citations_total INT NOT NULL,
  citations_ok    INT NOT NULL,
  citations_flagged JSONB,         -- list of {citation_idx, reason}
  action          TEXT NOT NULL,   -- 'pass' | 'downgrade' | 'reject'
  PRIMARY KEY (conviction_id, verified_at)
);
```

The allocator reads `action` to decide whether to size the position. A
`downgrade` (e.g., 3/5 citations passed) cuts conviction weight by half; a
`reject` zeros it.

---

## 7. Phased rollout

### Phase A — Foundation (week 1)

Goal: enable structured citation, even if agents don't use it yet.

- [ ] `db/schema.py`: add `evidence_snapshot` + `conviction_verification` tables
- [ ] `pipelines/schemas.py`: add `Citation` and update `ConvictionView.rationale` to accept either str (legacy) or structured `Rationale` (new). Soft migration.
- [ ] `pipelines/tool_dispatch.py`: wrap every tool return in an evidence-stamping decorator
- [ ] `compute_indicator(symbol, indicator, asof=None)` tool — RSI_14, RSI_28, BBAND_position, ATR_14, SMA cross states, computed from `local_bars`. Returns scalar + evidence_id.
- [ ] `query_news(symbol, terms, window)` tool — semantic + keyword search over `post` thread `news-headlines`. Returns post_ids + evidence_id.
- [ ] `verify_catalyst(event_text, date, window_days)` tool — semantic search for event-date pairs in news. Returns matches + evidence_id.

**Exit criteria**: from an MCP-tool smoke test, the four new tools work and write to evidence_snapshot. Agents not yet required to use them.

### Phase B — Skill library (week 2)

Goal: agent-callable, verified, reusable analysis code. **Conservative scope
(locked 2026-05-21)**: skills are human-authored Python files in a registry; the
LLM picks `from_skill="X"` but cannot synthesize new Python on the fly during
hourly reviews. Ad-hoc Python generation is deferred to the nightly
`*-model-tune` skills, where the same agent has a longer compute budget,
explicit human-in-the-loop review of new model code, and the promotion path
through `MODEL_CONTRACT.md`.

- [ ] `agents/<name>/skills/` directory pattern (sibling to `models/`)
- [ ] `meta_agent/skill_loader.py` — mirrors `model_loader.py`. Discovers skills, validates contract (`compute(args) -> {result, inputs_used}`).
- [ ] `run_skill(skill_name, **args)` tool — analogous to `submit_conviction_from_model` but for ad-hoc fact-finding (no DB writes, just evidence stamping). **Rejects skill_name not in the registry.**
- [ ] First three skills, copied from common patterns:
  - `compute_above_sma200(symbol)` — direct port of the trivial indicator
  - `compute_atr_14(symbol)` — for stop-loss sizing
  - `find_catalyst_in_news(symbol, terms)` — wrapper around query_news + filter
- [ ] Document the **skill promotion path**: ad-hoc skill written during nightly model_tune → used N times by an agent → promoted to model (with MODEL_VERSION, contract compliance). Same lifecycle Voyager uses, but the *authorship* of new skills happens in the nightly batch, not the hourly review.

**Exit criteria**: at least one sector agent (energy, say) successfully runs a `run_skill` call during a review and attaches the result as a citation. Hourly reviews remain on the registry-only path.

**Deferred to Phase E (post-MVP, exploratory)**: `eval_python` for ad-hoc agent-generated code at hourly cadence. Reasoning: agent-generated Python in the fast loop expands the blast radius significantly (sandbox correctness, evidence-stamping for one-off computations, prompt injection surface) without a clear win over a well-stocked skill library. Re-evaluate after Phase D has 30 days of operating data.

### Phase C — Verification worker (week 3)

Goal: independent CoVe-style verification on every non-flat conviction.

- [ ] `scripts/run_verify_worker.py` — consumes `verify_conviction` from agent_job queue
- [ ] Worker uses a separate `LLM_PROXY_URL` session — fresh context per check
- [ ] Cheap-model integration (Haiku) for the per-citation semantic check
- [ ] `conviction_verification` table populated; allocator reads `action` to gate sizing
- [ ] Dashboard tile: per-agent rolling 7-day verification pass rate

**Exit criteria**: 100% of non-flat convictions in the last 24h have a `conviction_verification` row. Agents with <50% pass rate get a Telegram nag.

### Phase D — Falsifiable theses (week 4)

Goal: hypotheses become testable code, not just rhetoric.

- [ ] `agent_thesis.falsification_spec` jsonb column
- [ ] `scripts/run_thesis_resolver.py` extended to execute `falsification_spec.skill(args)` and auto-grade based on `falsified_when` DSL
- [ ] Migration: existing `kind="hypothesis"` / `kind="prediction"` rows without falsification specs auto-downgraded to `kind="opinion"`. Forces agents to convert their forward-looking claims into testable form.
- [ ] The thesis review prompt instructs: a hypothesis without a falsification spec doesn't generate desk action.

**Exit criteria**: the next OPEC-June-4-style fabrication can't survive — `verify_catalyst("OPEC+ meeting", "2026-06-04")` returns 0 matches; the thesis's falsification spec runs and auto-flags it.

---

## 8. What we copy (not import) from open-source

### Voyager — MineDojo/Voyager (MIT-licensed, full code on GitHub)

**Copy the skill library structure.** Voyager's `skill_library/` is the closest
existing analog to what Phase B needs. Pattern to lift verbatim:

- One skill = one file under `skill_library/skill/code/<name>.js`
- Each skill has a paired `description/<name>.txt` (human-readable purpose)
- Embedding index over descriptions for top-K retrieval at runtime
- Re-execution from environment state to verify before reuse

Translate to Python+SQL: skills under `agents/<name>/skills/<name>.py`, paired
docstring (already standard Python), the embeddings index in pgvector (you
already have `embed-sweeper` for this on news).

What to skip from Voyager: its automatic-curriculum exploration loop is
designed for unbounded environments (Minecraft); your desk has a fixed
universe + clear allocator feedback. The skill library is the part that maps.

References:
- `github.com/MineDojo/Voyager/tree/main/skill_library`
- The "ever-growing skill library of executable code for storing and retrieving complex behaviors" pattern from the paper

### LATCH — paper only (no public code as of 2026-02-12 publication)

**Copy the semantic/execution split** as architecture, even though there's no
code to port. Their key sentence is the contract: *"patient-level data are
never exposed to large language models. Only schema metadata reaches LLM
components."*

For your desk: *bars-level data are never exposed to large language models.
Only computed scalars (via tool returns) reach LLM components.* Pin this in
the system prompt:

```
You receive numeric values ONLY as tool_result outputs, with evidence_ids
attached. You may not author numeric values in your rationale; any number
you cite must come from a tool you just called this turn.
```

This is the hardest line to enforce without architectural change; the Phase A
`compute_indicator` tool + Phase C verification worker together close it.

### Program-Aided Language Models (PaL) — reasoning-machines/pal on GitHub

**Copy the "LLM writes the program, runtime executes it" pattern — but defer
to the nightly model_tune path, not the hourly review.** PaL's contribution
was showing that LLMs are far better at *writing correct Python* than at
*computing correct arithmetic*. The repo has the prompt templates that elicit
code-only output. Pattern to lift later:

- For nightly model_tune sessions, the LLM writes a new skill (or refines an
  existing one) as a Python file. The harness validates against the skill
  contract and saves under `agents/<name>/skills/`.
- The skill is then available to that agent in the next hourly review via
  `run_skill` — but its body was written and committed by the nightly
  process, not invented mid-review.

This keeps the hourly review loop on the conservative registry-only path
while still enabling agent-authored analysis code through a separate,
longer-cadence channel with human checkpoints.

### ReAct — pattern, not a single codebase

The reason-act-observe interleaving is already what your `pipelines/tool_loop.py`
does. The pattern is canonical; no code to lift, just enforce the discipline:
LLM emits one tool call → observes result → emits next call or final answer.
Avoid "free-form reasoning paragraphs" between tool calls — they're where
hallucination compounds. Verbalize the reasoning *inside* the tool call's
description field if needed, where it's bounded.

### Open Interpreter — OpenInterpreter/open-interpreter on GitHub (AGPL-3.0)

**Copy the sandbox-execution patterns** for the `eval_python` tool above.
Specifically:

- Process isolation (subprocess + ulimit + timeout)
- Output capture (stdout/stderr separated)
- Whitelisted import allowlist
- Stateful session for follow-up queries within a single review

Their AGPL license means we copy patterns, not files. The sandbox setup is
small enough to re-implement from scratch using their structure as a guide.

### CoVe — from the original arxiv paper (no canonical repo, multiple impls)

**Copy the prompt structure** for the verification worker:

```
Stage 1: extract every claim from the conviction (numeric or event-based)
Stage 2: for each claim, generate a verification question  ASKED IN ISOLATION
Stage 3: answer each question using only the cited evidence
Stage 4: aggregate — was each claim supported?
```

Implementation is ~150 lines of Python wrapping cheap LLM calls. Worth doing
fresh; the value is the architectural pattern (isolation between stages), not
specific prompt phrasing.

---

## 9. Accessibility: how this stays readable to future agents

Each agent's review bundle today already includes prior theses, active views,
account context. Once this lands, the bundle also includes:

- The list of available **tools** (with one-line docstring each)
- The list of available **skills** (with one-line docstring + recent usage examples)
- A worked example: one prior turn where the agent used 3 tools, stamped 3 citations, and submitted a clean conviction
- Anti-patterns: one prior turn where an agent claimed RSI 75 without calling compute_indicator, and what got rejected

The system prompt also gains a section called **"How to cite"** that lays out the four citation kinds, the rejection rules, and one concrete example per kind. Mirrors the design of the existing `agents/MODEL_CONTRACT.md` (rules + examples + rationale, in that order).

Key readability commitments:
- No new YAML configs. New behavior lives in Python tool implementations + schema.
- Schemas live in one file (`pipelines/schemas.py`) and are pydantic — agents see them via the bundle's auto-generated JSON-schema docs.
- The "rejection reasons" surfaced to agents are descriptive: "you claimed RSI 75 on SCO but did not call compute_indicator this turn — call it first, then re-submit."

---

## 10. Adding a new citation kind (extension recipe)

The citation kind registry lives in `meta_agent/citation_pipeline.py`. Adding
a new kind is one new class + one registry line; no other files need to know.

**Step 1** — Subclass `CitationPipeline`:

```python
# meta_agent/citation_pipeline.py
class EarningsCalendarPipeline(CitationPipeline):
    """`kind='earnings_calendar'` — evidence is a confirmed earnings date
    from an external calendar feed."""
    kind = "earnings_calendar"

    async def verify(self, citation: dict) -> CheckResult:
        snap, fail = await _resolve_snapshot(citation, self.kind)
        if fail is not None:
            return fail
        # ... kind-specific replay check (e.g., re-fetch the calendar and
        # confirm the date is still listed)
        return CheckResult(True)
```

**Step 2** — Register it:

```python
_REGISTRY: dict[str, CitationPipeline] = {
    p.kind: p for p in (
        NewsPostPipeline(),
        ModelRunPipeline(),
        ComputedIndicatorPipeline(),
        PriorThesisPipeline(),
        SiblingViewPipeline(),
        EarningsCalendarPipeline(),   # ← new
    )
}
```

**Step 3** — Add the string to `pipelines/schemas.CITATION_KIND`:

```python
CITATION_KIND = Literal[
    "news_post", "model_run", "computed_indicator",
    "prior_thesis", "sibling_view",
    "earnings_calendar",   # ← new
]
```

If you forget Step 3, the module-load assertion in `pipelines/schemas.py`
fails loudly with a diff:

    RuntimeError: CITATION_KIND Literal drifted from citation_pipeline registry:
                  only-in-registry=['earnings_calendar'] ...

That assertion is the only thing keeping the two surfaces in lockstep. Don't
remove it.

**No other files need touching.** The MCP tool surface, the runner, the
worker, `db/store.stamp_evidence`, the conviction_verification flow — all
discover the new kind automatically through the registry.

---

## 11. TL;DR for the next reader

- LLM = intent. Harness = facts. Evidence_snapshot = audit trail.
- Every load-bearing claim → Citation pointing at evidence_snapshot.
- Verification is a separate worker with fresh LLM context (CoVe-isolated).
- `direction='flat'` is honored as legitimate; `'long'` without citations is rejected.
- **Hourly reviews are registry-only** — skills are pre-authored Python files the LLM selects from. **Agent-generated Python is restricted to nightly model_tune sessions** with human-in-the-loop. Re-evaluate after 30 days.
- Open-source patterns to copy verbatim: Voyager's skill library, Open Interpreter's sandbox (for the nightly path). LATCH is paper-only — copy the design split. CoVe is pattern, not code. PaL deferred to nightly.
- Migration is phased A→D, ~4 weeks. Each phase is independently shippable and observably improves desk faithfulness.
- **Citation kinds are one source of truth** — `meta_agent/citation_pipeline.py`. Adding a new kind is one class + one registry line (see §10). Module-load assertion in `pipelines/schemas.py` enforces lockstep with the Literal type.

---

*Architecture proposed 2026-05-21 following audit + literature review.
Companion docs: `MODEL_CONTRACT.md` (model-side contract),
`AGENT_LIFECYCLE.md` (process lifecycle).*
