# ProjectCorvus

A multi-agent autonomous trading desk built on the Claude Code CLI and the
Interactive Brokers Gateway. Ten sector agents publish *signed conviction
views*; a director ("Mike") sums them, sizes positions, and is the only
process allowed to talk to the broker.

```
sector agents (10)            Mike (allocator)         IBKR Gateway
─────────────────             ─────────────────       ───────────────
atlas, fab, fabless, rex,  →  read all convictions →  place_order
maya, titan, vera, trump,     normalize to weights    cancel_order
iron, volt                    enforce risk caps       (single client_id)
       │                              │                       │
       └──── conviction_view ─────────┘                       ▼
                                   ┌──────── Postgres ──────────┐
                                   │ agent_ledger, fills,       │
                                   │ nav_log, positions_anchor, │
                                   │ agent_state, conviction_*  │
                                   └────────────────────────────┘
                                              ▲
                                              │ Telegram chat-ops
                                       (concierge — local LLM)
```

## How it works

Each agent is a Claude Code skill scheduled via cron. On every hourly
review an agent loads its persona, the sector universe it owns
(`agents/sector_map.yaml`), recent news, technicals, and its own P&L —
then calls `submit_conviction_view(symbol, direction, conviction, …)`.
Convictions go to Postgres, **not** to the broker.

Mike runs hourly during market hours. He reads every active conviction,
normalizes signed shares into target weights, blends with the current
NAV anchor, and emits one batch of orders through the IBKR daemon. The
fill callback writes a per-agent *ledger* row for each contributing
agent — so realized + unrealized P&L is attributed back to the analyst
who advocated for the trade, even though the broker only knows about
Mike. Full mechanics live in [`DESK_POLICY.md`](DESK_POLICY.md).

## The agents

| Agent     | Coverage                                                                                          |
|-----------|---------------------------------------------------------------------------------------------------|
| `atlas`   | Macro — US indices, rates, FX, gold, dollar, international, vol, credit                            |
| `fab`     | Semiconductor manufacturing — foundries, equipment, memory, AI infra                              |
| `fabless` | Semiconductor designers + sector ETFs                                                              |
| `rex`     | Mega-cap tech ex-semi — cloud, ads, software, internet, payments                                  |
| `maya`    | Financials — banks, i-banks, brokers, cards, insurers, exchanges                                  |
| `titan`   | Energy + materials + commodities — integrated, refining, services, metals, chemicals              |
| `vera`    | Healthcare — pharma, biotech, med devices, tools, insurers                                         |
| `trump`   | Consumer staples + discretionary — food/bev, restaurants, apparel, autos, EVs, media              |
| `iron`    | Industrials + transports + defense                                                                 |
| `volt`    | Utilities + REITs + infrastructure                                                                 |
| `mike`    | **Director/allocator.** Reads convictions, places all orders. Morning brief + midday review.       |
| `cassidy` | Risk reviewer. End-of-day desk audit at 23:00 MST. No trading.                                     |

Sector agents have three skills each: `*-review` (hourly conviction
pass), `*-respond` (answer pending dashboard questions), and most have
`*-evening` and `*-model-tune` for nightly attribution + self-tuning.

## The per-agent ledger

There is no per-agent broker account. When Mike's order fills, the
daemon writes one **LEND** row per contributing agent with
`qty = fill_qty × normalized_share`. On the close it writes pro-rata
**RETURN** rows, each carrying a realized P&L computed against that
agent's weighted-average cost. Cash and commissions stay on Mike's book;
agents own only shares. See [`DESK_POLICY.md`](DESK_POLICY.md) §0 for
the full mechanics — including how dividends, inverse-ETF mapping, and
weighted-average preservation work.

## Concierge (Telegram chat-ops)

A long-running systemd-user service that owns the Telegram `getUpdates`
loop and routes inbound messages through the local LLM with a *read +
propose* tool surface. You can ask `"what's Rex's P&L today?"`, resolve
pending proposals from your phone, or file `"pause titan for tomorrow,
VIX is too low"` as a proposal. The concierge cannot place orders or
flip the kill switch by design — those still gate through approvals.
See [`concierge/README.md`](concierge/README.md).

## Repo layout

```
agents/             persona YAMLs + sector_map.yaml + inverse_etf_map.yaml
.claude/skills/     one Markdown skill per cron entry (atlas-review.md, …)
mcp_server.py       MCP tool surface (~150 tools — read, write, propose)
meta_agent/         allocator + ledger writer + IBKR daemon glue
ibkr/               ib_insync wrapper, daemon, RPC layer
db/                 schema.py + async pool; Postgres 18
data/               massive_client (Polygon-compatible market data + Benzinga news)
pipelines/          scheduled data ingestion (news, technicals, snapshots)
reporting/          combined_pnl, attribution, chart generators
concierge/          Telegram service (chat.py, router.py, tools.py)
scripts/            launchers, systemd units, install scripts
risk/               kill switch + risk caps
```

## Operational invariants

- **Single-writer to IBKR.** Only `mcp_server.rebalance_desk` opens the
  gateway. Every other consumer reads from Postgres. The gateway
  accepts one `client_id` per process; competing connections corrupt
  order state. (See `DESK_POLICY.md` §7.)
- **Combined P&L is the only P&L.** Always
  `get_my_pnl` / `get_pnl_summary` / `get_pnl_combined` —
  never report realized-only as a number.
- **Inverse-ETF routing.** Bearish views go long an inverse ETF, sized
  for its leverage. Direct shorts on underlyings are skipped.
- **Quiet window.** No conviction submissions 22:00–05:00 local. The
  allocator is paused; only Cassidy's evening review runs.
- **America/Phoenix.** Box has no DST; cron times are stable
  year-round. Mike's market-anchored skills are re-keyed twice a year
  in `config.yaml`.

## Branches

Pick one before cloning:

| Branch       | LLM backend                       | Hardware                  | Pick when                                     |
|--------------|-----------------------------------|---------------------------|-----------------------------------------------|
| **`main`**   | Anthropic API                     | Any Linux box             | Default. No GPU required.                     |
| `local-llm`  | Qwen3-32B-FP8 via vLLM            | CUDA GPU **≥ 40 GB VRAM** | You have a Blackwell/Ada/Hopper card.         |

Both share the same desk logic, schema, and scheduling. The only
difference is where `claude` CLI requests get answered.

## Install

```bash
git clone <repo-url> "$HOME/trading"
cd "$HOME/trading"
bash scripts/bootstrap.sh             # idempotent, ~5 min
# edit .env (TELEGRAM_BOT_TOKEN, MASSIVE_API_KEY, …)
.venv/bin/python scripts/preflight.py
```

`bootstrap.sh` detects the distro (Fedora/RHEL or Debian/Ubuntu),
installs system deps, creates the venv, generates `config.yaml` and
`.env`, sets up the Postgres role + schema, renders systemd user units
with your paths, and runs preflight. Manual pieces: install the
`claude` CLI, launch IBKR Gateway, set the box's timezone to
America/Phoenix. Full walkthrough in [`INSTALL.md`](INSTALL.md).

## Stack

Python 3.14 · Postgres 18 · Claude Code CLI · IBKR Gateway via
`ib_insync` · Massive (Polygon-compatible REST) for market data and
Benzinga news · vLLM + Qwen3-32B-FP8 on `local-llm`.
