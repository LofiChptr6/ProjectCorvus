"""PostgreSQL schema + connection pool. Called once at startup to create tables."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import asyncpg
import yaml

log = logging.getLogger(__name__)

# Legacy export kept so existing imports that reference DB_PATH don't break.
# The sqlite path is only used by the one-shot migration script.
DB_PATH = "data/trading.db"

_pool: Optional[asyncpg.Pool] = None
_pool_cfg: dict = {}


SCHEMA_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS audit_log (
        id                BIGSERIAL PRIMARY KEY,
        session_id        TEXT NOT NULL,
        created_at        TEXT NOT NULL,
        agent_name        TEXT NOT NULL,
        routine           TEXT NOT NULL,
        trigger_source    TEXT NOT NULL,
        system_prompt     TEXT,
        messages          TEXT NOT NULL,
        tool_rounds       INTEGER DEFAULT 0,
        final_response    TEXT,
        finish_reason     TEXT,
        duration_ms       INTEGER,
        prompt_tokens     INTEGER,
        completion_tokens INTEGER,
        error             TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS tool_calls (
        id           BIGSERIAL PRIMARY KEY,
        session_id   TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        tool_round   INTEGER NOT NULL,
        tool_name    TEXT NOT NULL,
        tool_input   TEXT NOT NULL,
        tool_output  TEXT,
        duration_ms  INTEGER,
        error        TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS orders (
        id               BIGSERIAL PRIMARY KEY,
        session_id       TEXT,
        agent_name       TEXT NOT NULL,
        created_at       TEXT NOT NULL,
        ibkr_order_id    INTEGER,
        symbol           TEXT NOT NULL,
        action           TEXT NOT NULL,
        order_type       TEXT NOT NULL,
        quantity         DOUBLE PRECISION NOT NULL,
        limit_price      DOUBLE PRECISION,
        stop_price       DOUBLE PRECISION,
        status           TEXT NOT NULL,
        risk_approved    INTEGER NOT NULL DEFAULT 0,
        human_approved   INTEGER,
        rejection_reason TEXT,
        reasoning        TEXT,
        mode             TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS fills (
        id            BIGSERIAL PRIMARY KEY,
        ibkr_exec_id  TEXT NOT NULL UNIQUE,
        order_id      INTEGER,
        agent_name    TEXT,
        filled_at     TEXT NOT NULL,
        symbol        TEXT NOT NULL,
        action        TEXT NOT NULL,
        quantity      DOUBLE PRECISION NOT NULL,
        fill_price    DOUBLE PRECISION NOT NULL,
        commission    DOUBLE PRECISION,
        exchange      TEXT,
        mode          TEXT NOT NULL,
        realized_pnl  DOUBLE PRECISION
    )""",
    "ALTER TABLE fills ADD COLUMN IF NOT EXISTS realized_pnl DOUBLE PRECISION",
    # NOTE: positions_snapshot + pnl_daily were deprecated by the per-agent
    # double-entry ledger redesign. See agent_ledger / agent_state below.
    """CREATE TABLE IF NOT EXISTS agent_allocations (
        id              BIGSERIAL PRIMARY KEY,
        agent_name      TEXT NOT NULL UNIQUE,
        allocation_pct  DOUBLE PRECISION NOT NULL,
        updated_at      TEXT NOT NULL,
        updated_by      TEXT NOT NULL DEFAULT 'cli'
    )""",
    # One-time migration from $-based to %-based. Idempotent — checks col existence.
    """DO $$ BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name='agent_allocations' AND column_name='allocated_usd'
        ) THEN
            ALTER TABLE agent_allocations ADD COLUMN IF NOT EXISTS allocation_pct DOUBLE PRECISION;
            -- Migrate using the most recent NAV from pnl_daily; fallback to safe default.
            UPDATE agent_allocations
            SET allocation_pct = COALESCE(allocation_pct, allocated_usd / NULLIF(43932.51, 0))
            WHERE allocation_pct IS NULL;
            ALTER TABLE agent_allocations ALTER COLUMN allocation_pct SET NOT NULL;
            ALTER TABLE agent_allocations DROP COLUMN allocated_usd;
        END IF;
    END $$;""",
    """CREATE TABLE IF NOT EXISTS kill_switch (
        id             BIGSERIAL PRIMARY KEY,
        agent_name     TEXT,
        is_active      INTEGER NOT NULL DEFAULT 0,
        activated_at   TEXT,
        activated_by   TEXT,
        reason         TEXT,
        deactivated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS news_items (
        id           BIGSERIAL PRIMARY KEY,
        fetched_at   TEXT NOT NULL,
        symbol       TEXT,
        headline     TEXT NOT NULL,
        article_id   TEXT,
        provider     TEXT,
        url          TEXT,
        body         TEXT,
        sentiment    TEXT,
        channels     TEXT[],
        published_at TIMESTAMPTZ
    )""",
    # Benzinga add-on (May 2026): surface extra metadata. Forward-only ALTERs;
    # existing rows get NULLs in the new columns. Idempotent — IF NOT EXISTS.
    "ALTER TABLE news_items ADD COLUMN IF NOT EXISTS url TEXT",
    "ALTER TABLE news_items ADD COLUMN IF NOT EXISTS body TEXT",
    "ALTER TABLE news_items ADD COLUMN IF NOT EXISTS sentiment TEXT",
    "ALTER TABLE news_items ADD COLUMN IF NOT EXISTS channels TEXT[]",
    "ALTER TABLE news_items ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ",
    # Dedup on Massive's article_id so the news ingestor can ON CONFLICT DO NOTHING.
    "CREATE UNIQUE INDEX IF NOT EXISTS news_items_article_id_uniq ON news_items (article_id) WHERE article_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS news_items_symbol_published_idx ON news_items (symbol, published_at DESC NULLS LAST)",
    # Phase A timely-news pipeline: categorize + agent-tag at ingest. category /
    # importance are derived from Benzinga channels + headline patterns;
    # agent_tags is snapshotted from sector_map so dashboard + context queries
    # don't reparse YAML on every render and ownership doesn't retroactively
    # rewrite if sector_map changes.
    "ALTER TABLE news_items ADD COLUMN IF NOT EXISTS category   TEXT",
    "ALTER TABLE news_items ADD COLUMN IF NOT EXISTS importance TEXT",
    "ALTER TABLE news_items ADD COLUMN IF NOT EXISTS agent_tags TEXT[]",
    "CREATE INDEX IF NOT EXISTS news_items_agent_tags_gin ON news_items USING GIN (agent_tags)",
    "CREATE INDEX IF NOT EXISTS news_items_importance_published_idx ON news_items (importance, published_at DESC NULLS LAST)",
    # GIN on post.meta enables agent-tag and article_id lookups via @> / ->>.
    # Used by find_post_by_article_id (news-headlines dedup).
    "CREATE INDEX IF NOT EXISTS post_meta_gin ON post USING GIN (meta)",
    # Phase B semantic recall (pgvector): columns + HNSW index are extension-
    # gated so this same SCHEMA_STATEMENTS runs cleanly whether pgvector is
    # installed or not. CREATE EXTENSION must be run separately by superuser:
    #     sudo -u postgres psql -d trading -c 'CREATE EXTENSION IF NOT EXISTS vector'
    """DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector') THEN
        ALTER TABLE news_items ADD COLUMN IF NOT EXISTS embedding   vector(1024);
        ALTER TABLE news_items ADD COLUMN IF NOT EXISTS embedded_at TIMESTAMPTZ;
        ALTER TABLE news_items ADD COLUMN IF NOT EXISTS embed_model TEXT;
      END IF;
    END $$""",
    # Idempotent index creation guarded the same way. CREATE INDEX requires the
    # `embedding` column to exist, hence the extension check.
    """DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector')
         AND EXISTS (SELECT 1 FROM information_schema.columns
                     WHERE table_name='news_items' AND column_name='embedding') THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_news_embedding_hnsw
                   ON news_items USING hnsw (embedding vector_cosine_ops)
                   WITH (m = 16, ef_construction = 64)
                   WHERE embedding IS NOT NULL AND published_at > ''2026-01-01''';
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_news_embed_pending
                   ON news_items (published_at DESC) WHERE embedding IS NULL';
      END IF;
    END $$""",
    # Per-agent thesis/memory journal (append-only). Status updates on existing
    # rows; new ideas append new rows. parent_id chains supersedes/refinements.
    """CREATE TABLE IF NOT EXISTS agent_thesis (
        id               BIGSERIAL PRIMARY KEY,
        agent_name       TEXT NOT NULL,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        kind             TEXT NOT NULL,
        title            TEXT NOT NULL,
        body             TEXT NOT NULL,
        status           TEXT NOT NULL DEFAULT 'open',
        verify_by        DATE,
        parent_id        BIGINT REFERENCES agent_thesis(id),
        market_snapshot  JSONB,
        resolution_note  TEXT,
        resolved_at      TIMESTAMPTZ
    )""",
    "CREATE INDEX IF NOT EXISTS idx_thesis_agent_status ON agent_thesis (agent_name, status, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_thesis_verify_open ON agent_thesis (agent_name, verify_by) WHERE status = 'open'",
    # Tool-gap requests routed through Mike (Mike consolidates in his morning analysis).
    """CREATE TABLE IF NOT EXISTS agent_tool_gaps (
        id           BIGSERIAL PRIMARY KEY,
        agent_name   TEXT NOT NULL,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        tool_name    TEXT NOT NULL,
        description  TEXT NOT NULL,
        use_case     TEXT NOT NULL,
        priority     TEXT NOT NULL DEFAULT 'normal',
        status       TEXT NOT NULL DEFAULT 'open',
        mike_note    TEXT,
        resolved_at  TIMESTAMPTZ
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tool_gaps_status ON agent_tool_gaps (status, created_at DESC)",
    # One row per agent per trading day for the evening summary.
    """CREATE TABLE IF NOT EXISTS agent_evening_digests (
        id                BIGSERIAL PRIMARY KEY,
        agent_name        TEXT NOT NULL,
        trading_date      DATE NOT NULL,
        created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        thesis_summary    TEXT,
        open_questions    TEXT,
        tomorrow_focus    TEXT,
        pnl_today         NUMERIC,
        pnl_week          NUMERIC,
        positions_json    JSONB,
        chart_path        TEXT,
        telegram_sent_at  TIMESTAMPTZ,
        UNIQUE(agent_name, trading_date)
    )""",
    # Conviction views — sector agents publish (symbol, direction, conviction)
    # rows that feed mike-allocator. Upserted on every review; conviction auto-
    # expires via expires_at so stale views never pollute the allocator.
    """CREATE TABLE IF NOT EXISTS agent_conviction (
        id                    BIGSERIAL PRIMARY KEY,
        agent_name            TEXT NOT NULL,
        symbol                TEXT NOT NULL,
        direction             TEXT NOT NULL,
        conviction            NUMERIC NOT NULL,
        expected_return_pct   NUMERIC,
        time_to_target_days   INTEGER,
        rationale             TEXT,
        model_inputs          JSONB,
        momentum_confirmed    BOOLEAN,
        submitted_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at            TIMESTAMPTZ NOT NULL,
        UNIQUE(agent_name, symbol)
    )""",
    "ALTER TABLE agent_conviction ADD COLUMN IF NOT EXISTS momentum_confirmed BOOLEAN",
    # Defensive auto-flat trigger: if the position's unrealized return falls
    # below -stop_pct, the allocator treats this conviction as flat regardless
    # of whether the agent re-publishes. Optional; NULL means "no stop." Useful
    # for inverse-ETF longs where decay compounds against late agent reactions.
    "ALTER TABLE agent_conviction ADD COLUMN IF NOT EXISTS stop_pct NUMERIC",
    # Entry-date anchor for position-aging context (anti-premature-drop). Set
    # to NOW() when the agent's direction on this symbol changes (flat↔non-flat
    # or long↔short); preserved across upserts when direction is unchanged. So
    # `NOW() - first_held_since` gives "days I've been committed to this view."
    "ALTER TABLE agent_conviction ADD COLUMN IF NOT EXISTS first_held_since TIMESTAMPTZ",
    "CREATE INDEX IF NOT EXISTS idx_conv_active ON agent_conviction (expires_at) WHERE conviction > 0",
    "CREATE INDEX IF NOT EXISTS idx_conv_symbol ON agent_conviction (symbol, expires_at)",
    # Forecast rows — proof-of-work research. Each agent publishes ≥20 rows per
    # hour from their sector universe regardless of whether they take action.
    # Allocator does NOT read this table; convictions remain the trade signal.
    # Each symbol can have up to 4 horizon rows: intraday (≤1d), near (2-5d),
    # far (6-30d), cycle (31+d). UNIQUE key is (agent_name, symbol, horizon).
    """CREATE TABLE IF NOT EXISTS agent_forecast (
        id                    BIGSERIAL PRIMARY KEY,
        agent_name            TEXT NOT NULL,
        symbol                TEXT NOT NULL,
        horizon               TEXT NOT NULL DEFAULT 'intraday',
        expected_return_pct   NUMERIC NOT NULL,
        likelihood            NUMERIC NOT NULL,
        time_to_target_days   INTEGER NOT NULL,
        forecast_score        NUMERIC NOT NULL,
        method                TEXT NOT NULL,
        rationale             TEXT,
        submitted_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at            TIMESTAMPTZ NOT NULL,
        UNIQUE(agent_name, symbol, horizon)
    )""",
    # Backfill horizon column for pre-existing tables created without it.
    "ALTER TABLE agent_forecast ADD COLUMN IF NOT EXISTS horizon TEXT NOT NULL DEFAULT 'intraday'",
    # Re-derive horizon from time_to_target_days for any row still at 'intraday'
    # default that was actually a longer-horizon forecast (one-time backfill;
    # idempotent — rows already on the correct non-'intraday' horizon are skipped).
    """UPDATE agent_forecast SET horizon = CASE
        WHEN time_to_target_days <= 1 THEN 'intraday'
        WHEN time_to_target_days <= 5 THEN 'near'
        WHEN time_to_target_days <= 30 THEN 'far'
        ELSE 'cycle'
    END WHERE horizon = 'intraday'""",
    # Drop old per-(agent,symbol) uniqueness; replace with per-(agent,symbol,horizon).
    "ALTER TABLE agent_forecast DROP CONSTRAINT IF EXISTS agent_forecast_agent_name_symbol_key",
    """DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'agent_forecast_agent_name_symbol_horizon_key'
    ) THEN
        ALTER TABLE agent_forecast
            ADD CONSTRAINT agent_forecast_agent_name_symbol_horizon_key
            UNIQUE (agent_name, symbol, horizon);
    END IF;
END $$""",
    "CREATE INDEX IF NOT EXISTS idx_forecast_active ON agent_forecast (agent_name, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_forecast_symbol ON agent_forecast (symbol, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_forecast_horizon ON agent_forecast (agent_name, symbol, horizon)",
    # Forecast-outcome columns — filled by scripts/run_forecast_resolver.py when
    # a forecast's horizon elapses. The resolver fetches daily bars from Massive,
    # computes realized return from submitted_at close → submitted_at+horizon close,
    # writes the answer here. NULL until resolved; reset to NULL on UPSERT so a
    # republished forecast starts a fresh outcome window.
    "ALTER TABLE agent_forecast ADD COLUMN IF NOT EXISTS realized_return_pct NUMERIC",
    "ALTER TABLE agent_forecast ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ",
    "ALTER TABLE agent_forecast ADD COLUMN IF NOT EXISTS resolution_source TEXT",
    "CREATE INDEX IF NOT EXISTS idx_forecast_unresolved ON agent_forecast (submitted_at, time_to_target_days) WHERE resolved_at IS NULL",
    # Mike's allocator decisions — one row per rebalance run.
    """CREATE TABLE IF NOT EXISTS allocation_decision (
        id                       BIGSERIAL PRIMARY KEY,
        decided_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        nav_at_decision          NUMERIC NOT NULL,
        target_weights_json      JSONB NOT NULL,
        contributing_views_json  JSONB NOT NULL,
        orders_placed_json       JSONB,
        notes                    TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_alloc_decided ON allocation_decision (decided_at DESC)",
    # ─────────────────────────────────────────────────────────────────────
    # Per-agent double-entry ledger. Replaces the old `agent_pnl_attribution`
    # + `holding_kanban` model. Each row is one accounting event in an agent's
    # book:
    #   - LEND: mike's allocator filled a BUY → fractional shares are lent
    #     to each contributing agent (qty proportional to their normalized
    #     conviction in `meta_agent.allocator.split_attribution`). cost basis
    #     = fill price.
    #   - RETURN: mike's allocator filled a SELL → close qty is distributed
    #     pro-rata across agents currently holding the symbol; each row's
    #     realized_pnl = qty × (sale_price − that_agent's_weighted_avg_cost).
    #   - DIVIDEND: corp-action feed credits each holder pro-rata to held qty.
    # Cash is NEVER on an agent's ledger — it stays on mike's book (nav_log).
    # See DESK_POLICY §0/§7 for the full model.
    """CREATE TABLE IF NOT EXISTS agent_ledger (
        id              BIGSERIAL PRIMARY KEY,
        booked_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        fill_id         BIGINT REFERENCES fills(id) ON DELETE SET NULL,
        decision_id     BIGINT REFERENCES allocation_decision(id) ON DELETE SET NULL,
        agent_name      TEXT NOT NULL,
        symbol          TEXT NOT NULL,
        event           TEXT NOT NULL CHECK (event IN ('LEND','RETURN','DIVIDEND')),
        qty             NUMERIC NOT NULL CHECK (qty > 0),
        price_per_share NUMERIC NOT NULL,
        realized_pnl    NUMERIC,
        notes           TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_agent_ledger_agent_at ON agent_ledger (agent_name, booked_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_agent_ledger_symbol_at ON agent_ledger (symbol, booked_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_agent_ledger_fill ON agent_ledger (fill_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_ledger_agent_symbol ON agent_ledger (agent_name, symbol, booked_at)",
    # Hourly materialized snapshot: one row per (agent, hour_bucket) with
    # cumulative P&L plus per-symbol detail in JSONB. The headline numbers
    # (realized_pnl, unrealized_pnl, total_pnl) are CUMULATIVE since inception
    # so day-over-day P&L is simply total_pnl(t1) − total_pnl(t0) — settlement
    # noise disappears because RETURNs move money from unrealized to realized
    # without changing the total. Refreshed by scripts/refresh_agent_state.py.
    """CREATE TABLE IF NOT EXISTS agent_state (
        id                BIGSERIAL PRIMARY KEY,
        snapshot_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        hour_bucket       TIMESTAMP GENERATED ALWAYS AS
                            (date_trunc('hour', snapshot_at AT TIME ZONE 'UTC')) STORED,
        agent_name        TEXT NOT NULL,
        realized_pnl      NUMERIC NOT NULL,
        unrealized_pnl    NUMERIC NOT NULL,
        total_pnl         NUMERIC NOT NULL,
        open_cost         NUMERIC NOT NULL,
        open_market_value NUMERIC NOT NULL,
        n_positions       INTEGER NOT NULL,
        positions_json    JSONB NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_agent_state_at ON agent_state (snapshot_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_agent_state_agent_at ON agent_state (agent_name, snapshot_at DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_state_hour_unique ON agent_state (agent_name, hour_bucket)",
    # Cash + NAV anchor written by mike's allocator every rebalance. The
    # deterministic kanban refresh script (scripts/refresh_kanban.py) reads
    # the latest row, then applies fills since `recorded_at` to derive
    # current cash without touching the IBKR gateway. mike is the only
    # writer; all other paths are read-only.
    """CREATE TABLE IF NOT EXISTS nav_log (
        id            BIGSERIAL PRIMARY KEY,
        recorded_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        decision_id   BIGINT REFERENCES allocation_decision(id) ON DELETE SET NULL,
        desk_nav      NUMERIC NOT NULL,
        cash_balance  NUMERIC NOT NULL,
        source        TEXT NOT NULL DEFAULT 'mike'
    )""",
    "CREATE INDEX IF NOT EXISTS idx_nav_log_recorded ON nav_log (recorded_at DESC)",
    # Per-symbol position anchor written by mike's allocator every rebalance.
    # Mirrors `nav_log` for the position leg: mike captures IBKR-canonical
    # quantities into `snapshot_json` ({"AAPL": 100, "MSFT": 50, ...}); the
    # deterministic refresh starts from the latest anchor and applies fills
    # made *after* `recorded_at`. This avoids the fills-vs-IBKR drift we hit
    # when the fills table was missing pre-system fills (e.g. XLF: IBKR shows
    # 105 shares, fills only have 90) or carried orphan SLDs without matching
    # BOTs (e.g. VOO: 1 SLD, no BOT, refresh thought desk was short 1).
    """CREATE TABLE IF NOT EXISTS positions_anchor (
        id            BIGSERIAL PRIMARY KEY,
        recorded_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        decision_id   BIGINT REFERENCES allocation_decision(id) ON DELETE SET NULL,
        snapshot_json JSONB NOT NULL,
        source        TEXT NOT NULL DEFAULT 'mike'
    )""",
    "CREATE INDEX IF NOT EXISTS idx_positions_anchor_recorded ON positions_anchor (recorded_at DESC)",
    # Per-agent narrative archive. Written by sector-archivist weekly. Each row
    # is one chapter covering [period_start, period_end] for one agent —
    # condensing closed theses, conviction history, and attributed P&L into a
    # short prose summary so old rows can be pruned without losing the story.
    """CREATE TABLE IF NOT EXISTS sector_story (
        id              BIGSERIAL PRIMARY KEY,
        agent_name      TEXT NOT NULL,
        period_start    DATE NOT NULL,
        period_end      DATE NOT NULL,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        narrative       TEXT NOT NULL,
        stats_json      JSONB,
        rows_archived   JSONB,
        UNIQUE(agent_name, period_end)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_story_agent ON sector_story (agent_name, period_end DESC)",
    # Desk-wide threads board. Multi-author public bulletin: user announcements,
    # Mike's morning analysis, agent reports, external news feeds. Read by every
    # agent on every review (active desk-announcements posts are auto-injected
    # into agent system prompts).
    """CREATE TABLE IF NOT EXISTS thread (
        id            BIGSERIAL PRIMARY KEY,
        slug          TEXT NOT NULL UNIQUE,
        title         TEXT NOT NULL,
        description   TEXT,
        tags          TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        archived_at   TIMESTAMPTZ
    )""",
    """CREATE TABLE IF NOT EXISTS post (
        id              BIGSERIAL PRIMARY KEY,
        thread_id       BIGINT NOT NULL REFERENCES thread(id) ON DELETE CASCADE,
        author          TEXT NOT NULL,
        author_kind     TEXT NOT NULL,
        posted_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        title           TEXT,
        body            TEXT NOT NULL,
        meta            JSONB NOT NULL DEFAULT '{}'::jsonb,
        parent_post_id  BIGINT REFERENCES post(id) ON DELETE SET NULL,
        expires_at      TIMESTAMPTZ
    )""",
    "CREATE INDEX IF NOT EXISTS idx_post_thread_posted ON post (thread_id, posted_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_post_author_posted ON post (author, posted_at DESC)",
    # Seed canonical threads (idempotent — INSERT ON CONFLICT DO NOTHING).
    """INSERT INTO thread (slug, title, description, tags) VALUES
        ('desk-announcements',
         'Desk announcements',
         'Operational constraints + system-wide notices. Every agent reads active posts on every review.',
         ARRAY['announcements','ops']),
        ('mikes-morning',
         'Mike''s morning analysis',
         'Director-level daily market read. Mike posts ~9:06 ET pre-open.',
         ARRAY['analysis','daily']),
        ('user-announcements',
         'User announcements',
         'Posts directly from the desk owner.',
         ARRAY['user']),
        ('news-headlines',
         'External news feed',
         'Auto-posted headlines from connected news sources.',
         ARRAY['news','external'])
       ON CONFLICT (slug) DO NOTHING""",
    # Hot-path indexes (audit_log/tool_calls/orders/fills): without these the query
    # planner falls back to seq-scans once these tables grow past ~100k rows.
    "CREATE INDEX IF NOT EXISTS idx_audit_log_session ON audit_log (session_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_log_agent_created ON audit_log (agent_name, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls (session_id)",
    # Observability (obs/proxy.py) writes every /v1/messages exchange to audit_log.
    # Multiple exchanges share one session_id (tool-use loop), distinguished by request_index.
    # thinking_block holds Qwen's <think>…</think> content split out from the final text.
    "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS skill_name TEXT",
    "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS request_index INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS thinking_block TEXT",
    "CREATE INDEX IF NOT EXISTS idx_audit_log_skill_recent ON audit_log (skill_name, created_at DESC) WHERE skill_name IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_orders_agent_status ON orders (agent_name, status)",
    "CREATE INDEX IF NOT EXISTS idx_orders_created ON orders (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_fills_agent_filled ON fills (agent_name, filled_at DESC)",
    # Per-agent inbox — questions the user posts from the dashboard chat.
    # The matching `*-respond` skill picks up rows where responded_at IS NULL,
    # writes response_body, and stamps responded_at. The table predates the
    # schema-managed bootstrap; CREATE TABLE IF NOT EXISTS makes this a no-op
    # on databases that already have it.
    """CREATE TABLE IF NOT EXISTS agent_inbox (
        id                  BIGSERIAL PRIMARY KEY,
        agent_name          TEXT NOT NULL,
        sender              TEXT NOT NULL DEFAULT 'user',
        body                TEXT NOT NULL,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        triggered_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        responded_at        TIMESTAMPTZ,
        response_body       TEXT,
        response_session_id TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_inbox_pending ON agent_inbox (agent_name, created_at DESC) WHERE responded_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_inbox_recent ON agent_inbox (agent_name, created_at DESC)",
    # Shadow tables — mirror agent_conviction / agent_forecast for the new
    # Python pipeline's dry-run mode. Reviews written in shadow mode go here
    # so we can diff against the live conviction/forecast tables (harness
    # output) before flipping the cutover. NO foreign keys, NO uniqueness on
    # symbol — multiple shadow runs in a row are allowed for the same hour.
    """CREATE TABLE IF NOT EXISTS agent_conviction_shadow (
        id                    BIGSERIAL PRIMARY KEY,
        agent_name            TEXT NOT NULL,
        symbol                TEXT NOT NULL,
        direction             TEXT NOT NULL,
        conviction            NUMERIC NOT NULL,
        expected_return_pct   NUMERIC,
        time_to_target_days   INTEGER,
        rationale             TEXT,
        model_inputs          JSONB,
        momentum_confirmed    BOOLEAN,
        stop_pct              NUMERIC,
        submitted_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at            TIMESTAMPTZ NOT NULL,
        run_session_id        TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_conv_shadow_agent_run ON agent_conviction_shadow (agent_name, submitted_at DESC)",
    """CREATE TABLE IF NOT EXISTS agent_forecast_shadow (
        id                    BIGSERIAL PRIMARY KEY,
        agent_name            TEXT NOT NULL,
        symbol                TEXT NOT NULL,
        horizon               TEXT NOT NULL,
        expected_return_pct   NUMERIC NOT NULL,
        likelihood            NUMERIC NOT NULL,
        time_to_target_days   INTEGER NOT NULL,
        forecast_score        NUMERIC NOT NULL,
        method                TEXT NOT NULL,
        rationale             TEXT,
        submitted_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at            TIMESTAMPTZ NOT NULL,
        run_session_id        TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_forecast_shadow_agent_run ON agent_forecast_shadow (agent_name, submitted_at DESC)",
    # Unified Telegram message log. One row per inbound or outbound Telegram event.
    # `kind` partitions the stream into three audiences:
    #   - concierge LLM sees rows with kind IN ('user_text','concierge_reply','concierge_tool')
    #   - approval flow uses kind='approval' (inbound /y/n/buttons + outbound pings/confirmations)
    #   - everything else (agent reports, digests, charts) is kind='push' and is invisible to
    #     the concierge's chat context — fetched on demand via tools when needed.
    # role/tool_calls/tool_call_id are OpenAI chat-completions shape, allowing direct
    # replay into the LLM `messages` array without translation.
    """CREATE TABLE IF NOT EXISTS telegram_message (
        id                  BIGSERIAL PRIMARY KEY,
        direction           TEXT NOT NULL CHECK (direction IN ('inbound','outbound')),
        kind                TEXT NOT NULL CHECK (kind IN (
                              'user_text','slash_cmd','approval','push',
                              'concierge_reply','concierge_tool'
                            )),
        chat_id             TEXT,
        telegram_message_id BIGINT,
        role                TEXT,
        content             TEXT,
        tool_calls          JSONB,
        tool_call_id        TEXT,
        meta                JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS idx_telegram_message_kind_id ON telegram_message (kind, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_telegram_message_chat_id_id ON telegram_message (chat_id, id DESC)",
]


def _load_pg_cfg() -> dict:
    """Load pg creds from config.yaml (override via PG_* env vars)."""
    global _pool_cfg
    if _pool_cfg:
        return _pool_cfg
    cfg_path = Path("config.yaml")
    pg: dict = {}
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            pg = (yaml.safe_load(f) or {}).get("postgres", {})
    _pool_cfg = {
        "host": os.getenv("PG_HOST") or pg.get("host", "localhost"),
        "port": int(os.getenv("PG_PORT") or pg.get("port", 5432)),
        "database": os.getenv("PG_DATABASE") or pg.get("database", "trading"),
        "user": os.getenv("PG_USER") or pg.get("user", "postgres"),
        "password": os.getenv("PG_PASSWORD") or str(pg.get("password", "")),
        "min_size": int(pg.get("min_pool", 1)),
        "max_size": int(pg.get("max_pool", 5)),
        "command_timeout": float(pg.get("command_timeout", 15)),
    }
    return _pool_cfg


class _BoundedAcquirePool:
    """Proxy around an asyncpg.Pool that defaults `acquire()` to a 10 s
    timeout, so a stuck/exhausted pool surfaces an `asyncio.TimeoutError`
    instead of blocking forever. Every MCP tool funnels through this
    pool; without the timeout, a single zombie connection can hang any
    tool and therefore hang the Claude Code worker ("session stopped
    responding"). Existing call sites that do `async with pool.acquire()
    as c:` inherit the default automatically. Callers that legitimately
    need a different bound can still pass `timeout=N` explicitly.

    A proxy is used instead of a direct monkey-patch because
    `asyncpg.Pool` defines `__slots__`, so `pool.acquire = wrapped`
    raises AttributeError. All non-`acquire` attributes delegate to the
    underlying pool via `__getattr__`."""

    __slots__ = ("_pool",)

    def __init__(self, pool: asyncpg.Pool):
        object.__setattr__(self, "_pool", pool)

    def acquire(self, *args, **kwargs):
        kwargs.setdefault("timeout", 10)
        return self._pool.acquire(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._pool, name)


async def get_pool() -> asyncpg.Pool:
    """Return the shared asyncpg pool (wrapped in `_BoundedAcquirePool`),
    creating it on first call. See `_BoundedAcquirePool` for the
    motivation behind the wrapper."""
    global _pool
    if _pool is not None:
        return _pool
    cfg = _load_pg_cfg()
    raw = await asyncpg.create_pool(
        host=cfg["host"],
        port=cfg["port"],
        database=cfg["database"],
        user=cfg["user"],
        password=cfg["password"],
        min_size=cfg["min_size"],
        max_size=cfg["max_size"],
        timeout=10,
        command_timeout=cfg["command_timeout"],
        # Recycle idle connections every 60s (default 300s). Defends against
        # silently-dead idle conns (Postgres server timeout, network blip)
        # in a long-running MCP process.
        max_inactive_connection_lifetime=60,
    )
    _pool = _BoundedAcquirePool(raw)  # type: ignore[assignment]
    return _pool  # type: ignore[return-value]


async def init_db(db_path: str = DB_PATH) -> None:
    """Create all tables and seed initial kill_switch row if empty.
    `db_path` is ignored (kept for backward compat with callers)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        for stmt in SCHEMA_STATEMENTS:
            await conn.execute(stmt)
        # Seed one global-kill row if table is empty (was 'INSERT OR IGNORE' in sqlite).
        row = await conn.fetchrow("SELECT COUNT(*) AS n FROM kill_switch")
        if row and row["n"] == 0:
            await conn.execute(
                "INSERT INTO kill_switch (agent_name, is_active) VALUES (NULL, 0)"
            )
    log.info("Postgres schema ready (host=%s db=%s)", _pool_cfg.get("host"), _pool_cfg.get("database"))


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
