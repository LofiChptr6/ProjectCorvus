# Desk Operating Policy

All sector agents must follow these rules on every review cycle. Call `get_desk_policy()` at the start of any session to load the current version.

---

## 0. The Per-Agent Ledger Model  ⟵ READ THIS FIRST

The desk runs on a **double-entry per-agent ledger**. Each agent has their own book that starts at $0 and accrues realized + unrealized P&L based on shares mike "lends" them per their conviction.

**Mechanics:**
- Mike's allocator (the *only* path that talks to the IBKR gateway) sums every active conviction across agents, normalizes target weights, and places orders at the broker.
- When a fill lands, the IBKR daemon's `_on_fill` callback (`meta_agent/ledger_writer.py`) writes `agent_ledger` events:
  - **BUY → LEND** rows. One row per contributing agent. `qty = fill_qty × normalized_share`. `price_per_share = fill_price`. The agent now "owes" mike that cost.
  - **SELL → RETURN** rows. The fill qty is distributed **pro-rata across agents currently holding the symbol** (per user decision; see §7). Each row carries `realized_pnl = qty × (sale_price − that_agent's_weighted_avg_cost)`.
  - **DIVIDEND** rows are credited pro-rata to current holders (cash physically lands on mike's book).
- **Cash never appears on an agent's ledger.** It stays exclusively on mike's book (`nav_log`). Agents own shares, not cash.
- **Commissions are absorbed by mike** — they do not reduce agent realized P&L.

**Cumulative semantics:**
- `realized_pnl` = lifetime sum of every RETURN/DIVIDEND row's `realized_pnl` for an agent.
- `unrealized_pnl` = `Σ qty × (current_mark − weighted_avg_cost)` over open positions; mark from Polygon (`data.massive_client.get_quote`).
- `total_pnl = realized_pnl + unrealized_pnl`. **Day-over-day P&L = `total_pnl(t1) − total_pnl(t0)`** — clean, because settlement just moves money between the realized and unrealized buckets without changing the sum.

**Weighted-average cost** is preserved across pro-rata closes (the RETURN row deducts cost at the current avg, not the sale price). This matches IBKR's `avg_cost` convention.

---

## 1. P&L Reporting

**Always use `get_my_pnl(agent_name="<you>")` for your own P&L.** Reads the latest `agent_state` snapshot (refreshed hourly + on every mike rebalance).

- Returns `realized_pnl`, `unrealized_pnl`, `total_pnl`, `n_positions`, `snapshot_at`.
- Report all three lines: `realized: $X | unrealized: $Y | total: $Z`.
- For windowed P&L (1d / wtd / 1m / 3m), use `get_agent_pnl_windows(agent_name=...)`.
- For per-event audit (which fill, which decision, which realized number), use `get_agent_ledger(agent_name=...)`.
- $0 total with open positions is a data problem, not a quiet day — flag it.

---

## 2. Conviction Submission Hygiene

Every conviction view **≥ 0.5 conviction** must be backed by at least two tool calls before submission:

| Required | Acceptable substitutes |
|----------|----------------------|
| `compute_technicals` | `get_bars` with manual RSI/SMA calc |
| `get_bars` | — |
| `get_news` | `get_upcoming_catalysts` for event-driven plays |

- Populate `model_inputs` with the raw output (RSI value, BBAND levels, headline).
- Record a `record_thesis` entry before submitting any view you intend to grade later.
- Gut-feel submissions with thin rationale are not prohibited, but must be labelled as such in the rationale field: "GUT-FEEL: …"

---

## 3. Bearish Expression — Inverse ETF Only

Express bearish sector views by going **long a chosen inverse ETF** (e.g. `direction="long"` on SQQQ, not `direction="short"` on QQQ). Direct shorts on underlyings are not supported.

- Divide conviction by the ETF's leverage multiple before submitting (3× inverse → conviction ÷ 3).
- If no suitable inverse ETF exists in the catalog, submit `direction="flat"` and raise a tool gap via `raise_tool_gap`.
- The `agent_ledger` records the *held* symbol (e.g. SQQQ), not the originating bearish view (QQQ). The ledger writer handles the inverse-ETF mapping when looking up contributors.

---

## 4. Attribution Awareness

You do **not** have your own broker fills. You have **lent shares** on mike's book.

- Your share count for a symbol = `Σ LEND.qty − Σ RETURN.qty` over your `agent_ledger` history. Fractional shares are normal — they reflect your normalized share of conviction at fill time.
- Your realized P&L on a closing fill = `your_returned_qty × (sale_price − your_weighted_avg_cost)`. Attributed automatically when mike's SELL fills.
- Your unrealized P&L = `your_qty × (current_mark − your_avg_cost)`. Refreshed hourly into `agent_state`.
- An agent who held a position whose conviction has expired still owns the lent shares until mike closes them. Your P&L keeps accruing.

---

## 5. Evening Review Checklist

Every `*-evening` run must complete all 7 steps:

1. Load — `get_my_pnl` + `get_agent_ledger` + `get_my_journal` + `get_my_active_views`
2. Grade — resolve all predictions due today with `update_thesis_status`
3. Tool audit — score each view data-backed vs gut-feel; flag recurring gaps
4. Plan — `record_thesis` for tomorrow's setups
5. Chart — `generate_agent_chart`
6. Telegram — `send_telegram_chart` with structured caption
7. Digest — `record_evening_digest` with `chart_path` and numeric P&L

Do not skip steps 5–7. Cassidy reads `agent_evening_digests` for her risk review.

---

## 6. Quiet Window

No conviction submissions between **10 PM – 5 AM local** (market closed, allocator is paused). Evening reviews may still run; chart/Telegram/digest steps are always permitted.

---

## 7. Read Paths and the IBKR Gateway

**Mike's allocator (`mcp_server.rebalance_desk`) is the only path allowed to talk to the IBKR gateway.** Every other consumer — sector agents, ad-hoc Claude sessions, briefing tools, analytics scripts, Telegram concierge — must read from Postgres only. The gateway accepts one `client_id` at a time per process; competing connections silently knock mike off and corrupt order state.

**Where to read what:**

| Question | Source |
|---|---|
| What is each agent's current P&L? | `agent_state` (latest row per agent) — UPSERTed hourly + on every mike rebalance |
| What does each agent hold? | `agent_state.positions_json` (latest snapshot) — array of `{sym, qty, avg_cost, mark, unrealized}` |
| Per-event audit trail (lend / return) | `agent_ledger` — every accounting event with fill_id and decision_id |
| Per-symbol current open quantity | `positions_anchor.snapshot_json` (latest) + signed `fills` since `recorded_at` |
| Live quote for any symbol | `data.massive_client.get_quote(symbol)` (Polygon-compatible REST) |
| Cash + NAV anchor | `nav_log` — most recent row, written by mike on every rebalance |
| Position anchor | `positions_anchor` — most recent row, written by mike on every rebalance |

**Closing methodology (pro-rata).** When mike SELLs `Q` shares of `S`:
1. Look up current holders of `S` from `agent_ledger` (`SUM(LEND.qty − RETURN.qty)` per agent).
2. Distribute `Q` proportional to each agent's current qty — agent's `RETURN.qty = Q × (their_qty / total_qty)`.
3. Each per-agent `RETURN.realized_pnl = qty × (sale_price − their_weighted_avg_cost)`.
4. Pro-rata closes preserve each agent's avg_cost (cost is deducted at the running avg, not the sale price), so future unrealized math stays consistent.

If no agent currently holds `S` (e.g. orphan IBKR position from before deploy), the close stays on mike's book — no agent_ledger rows are written.

**State refresh.** `agent_state` is rebuilt every hour by `scripts/refresh_agent_state.py` (cron `5 * * * *`, every day, regardless of trading hours), and again immediately after every mike rebalance. Pure deterministic Python — no LLM, no IBKR call. Reads `agent_ledger` + Polygon prices + `nav_log` for the desk-NAV reconciliation. Multiple writes within the same hour UPSERT on `(agent_name, hour_bucket)` — latest write wins.

**Why anchors (not raw fill sums) for desk-level position truth.** The `fills` table can be incomplete vs IBKR's actual book — pre-system trades, manual transactions, or orphan SLDs without matching BOTs would otherwise produce phantom positions. Anchoring against IBKR-canonical positions on every rebalance and only applying *deltas* since the anchor sidesteps the drift, while preserving "only mike sees the broker." For per-agent state we don't anchor — we trust the ledger because it's append-only and grows from a clean $0 with every fill written by `_on_fill`.

**If you find yourself wanting to call `ibkr.account.*` outside mike's allocator, it's a bug.** There is no scenario where a read query needs a fresh IBKR connection.
