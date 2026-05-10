"""Pydantic models for structured-output write paths.

The review/evening/model_tune skills emit a final assistant message that MUST
parse as JSON conforming to one of these models. The runner validates with
pydantic and batch-writes via existing db.store functions on success.

Phase 1 (respond) does not use structured output — write happens via the
`mark_inbox_responded` tool dispatch.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ── Review (Phase 2) ──────────────────────────────────────────────────────────


class ConvictionView(BaseModel):
    """One row in the agent's published conviction stack.

    Mirrors agent_conviction columns. `direction='flat'` + `conviction=0` is the
    canonical 'I have no view' submission and is acceptable per upsert_conviction
    docstring.
    """
    symbol: str
    direction: Literal["long", "flat"]
    conviction: float = Field(..., ge=0.0, le=1.0)
    expected_return_pct: Optional[float] = None
    time_to_target_days: Optional[int] = Field(None, ge=0)
    rationale: Optional[str] = None
    model_inputs: Optional[dict[str, Any]] = None
    momentum_confirmed: Optional[bool] = None
    stop_pct: Optional[float] = Field(None, ge=0.0)
    expires_in_hours: int = Field(default=1, ge=1, le=336)  # 14d max

    @field_validator("symbol")
    @classmethod
    def _upper_symbol(cls, v: str) -> str:
        return v.upper().strip()


class ForecastRow(BaseModel):
    """One forecast row. Each (symbol, horizon) pair is independent."""
    symbol: str
    expected_return_pct: float
    likelihood: float = Field(..., ge=0.0, le=1.0)
    time_to_target_days: int = Field(..., ge=0)
    method: str = "model"
    rationale: Optional[str] = None
    horizon: Optional[Literal["intraday", "near", "far", "cycle"]] = None

    @field_validator("symbol")
    @classmethod
    def _upper_symbol(cls, v: str) -> str:
        return v.upper().strip()


class ThesisRecord(BaseModel):
    """Append-only journal entry."""
    kind: Literal["hypothesis", "prediction", "observation", "question"]
    title: str = Field(..., max_length=200)
    body: str
    verify_by: Optional[str] = None  # YYYY-MM-DD
    parent_id: Optional[int] = None
    market_snapshot: Optional[dict[str, Any]] = None


class ThesisGrade(BaseModel):
    """Update an existing open thesis to confirmed/wrong/superseded."""
    thesis_id: int
    status: Literal["confirmed", "wrong", "superseded"]
    resolution_note: str


class ReviewOutput(BaseModel):
    """The structured payload an `*-review` skill emits as its final message.

    The orchestrator parses this, validates it, and dispatches the writes:
        clear_agent_convictions / upsert_conviction
        clear_agent_forecasts / upsert_forecasts_batch
        record_thesis / update_thesis_status
    All atomic at the row level — partial failures are logged and skipped, not
    cascaded.
    """
    convictions: list[ConvictionView] = Field(default_factory=list)
    forecasts: list[ForecastRow] = Field(default_factory=list)
    theses_to_record: list[ThesisRecord] = Field(default_factory=list)
    theses_to_grade: list[ThesisGrade] = Field(default_factory=list)
    telegram_summary: Optional[str] = None
    stdout_summary: Optional[str] = None


# ── Evening (Phase 3) ─────────────────────────────────────────────────────────


class EveningOutput(BaseModel):
    """Inputs the orchestrator feeds to generate_evening_slide + record_evening_digest."""
    headline: str
    trends: list[str] = Field(default_factory=list)
    theses: list[str] = Field(default_factory=list)
    philosophy: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    pnl_today: Optional[float] = None
    pnl_week: Optional[float] = None
    telegram_caption: Optional[str] = None
    theses_to_record: list[ThesisRecord] = Field(default_factory=list)
    theses_to_grade: list[ThesisGrade] = Field(default_factory=list)


# ── Model-tune (Phase 4) ──────────────────────────────────────────────────────


class ModelFileAction(BaseModel):
    """One file mutation. Orchestrator's file_ritual.py runs backup → write →
    import-check → smoke-test, with rollback on any step's failure."""
    action: Literal["tune", "add", "scrap"]
    file_path: str  # e.g. "agents/atlas/models/regime_score.py"
    new_content: Optional[str] = None  # required for tune/add; None for scrap
    new_version: Optional[str] = None  # required MODEL_VERSION for tune/add
    reason: str

    @field_validator("file_path")
    @classmethod
    def _path_must_be_under_models(cls, v: str) -> str:
        # Light defense — orchestrator additionally walks the path before writes.
        if "../" in v or v.startswith("/"):
            raise ValueError("file_path must be a relative path under agents/<sector>/models/")
        return v


class ModelTuneOutput(BaseModel):
    """Final structured payload from `*-model-tune` skill."""
    file_actions: list[ModelFileAction] = Field(default_factory=list, max_length=2)
    hypothesis_log_update: str  # full new contents for model_hypothesis.md
    thesis: ThesisRecord  # the kind="model_change" thesis to record
    telegram_summary: str
    stdout_summary: Optional[str] = None
