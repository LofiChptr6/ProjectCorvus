# Desk Operating Policy

All sector agents must follow these rules on every review cycle. Call `get_desk_policy()` at the start of any session to load the current version.

---

## 1. P&L Reporting — Unrealized Counts

**Always use `get_my_pnl(agent_name="<you>")` for your own P&L.** Never use `get_pnl_summary` alone for your personal number — it can miss unrealized P&L when conviction views have expired.

- `get_my_pnl` returns realized + unrealized combined, attributed via stored fill shares that survive conviction expiry.
- Report both lines: `realized: $X | unrealized: $Y | total: $Z`.
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

---

## 4. Attribution Awareness

You do **not** have fills. You have attribution shares on Mike's fills.

- Your P&L = sum of `(fill_pnl × your_attribution_share)` across all decisions where you contributed conviction.
- Unrealized P&L is your share of open position mark-to-market at the current IBKR price.
- If your attribution rows show `attributed_pnl: null`, the position is still open — that's unrealized, not zero.

---

## 5. Evening Review Checklist

Every `*-evening` run must complete all 7 steps:

1. Load — `get_my_pnl` + `get_agent_pnl_attribution` + `get_my_journal` + `get_my_active_views`
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
