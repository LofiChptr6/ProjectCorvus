# Thinking / reporting template (cross-agent)

Every sector agent (atlas, energy, commodity, fab, fabless, rex, maya, vera,
trump, iron, volt) MUST emit reasoning in this shape when they finish a run,
regardless of trigger. Mike + Cassidy follow their own templates and do not
read this file.

Skills that consume this template:
- All `*-review` skills (hourly + OCAP-triggered re-fires)
- All `*-model-tune` skills (when a model file was edited)

## Triggers this applies to

- **Hourly review** — scheduled top-of-hour `*-review` skill.
- **OCAP-triggered** — `ocap_triggered_review` job fired by
  `analysis.indicators_ocap` on a 5-min bar close (rolling_std_breach /
  bollinger_break / rsi_extreme / etc., per `agents/ocap_rules.yaml`).
  When this is your trigger, the job payload includes `symbol` and
  `triggers_seen` — surface both in your header.
- **Model-tune** — `*-model-tune` skill that touched a
  `agents/<name>/models/*.py` file must ALSO fire the code-adjustment
  telegram block below.

## Required reasoning before you send

For each symbol you'll highlight in the output you MUST have:

1. **An actual indicator read** — pulled from `compute_technicals` /
   `compute_all_models` THIS RUN. No remembered numbers. If a number isn't
   from this run's tool result, don't quote it.
2. **A news pull** — `get_news(symbol)` called THIS RUN regardless of price
   move. If empty, say so and quote the search timestamp.
3. **At least one quant-model output** — from `compute_all_models`. If
   models errored or all returned flat on this name, say that explicitly
   and lean on technicals + fundamentals only (per `[BROKEN MODEL
   DECISION RULE]` in your system prompt).
4. **A multi-horizon view** — short (next hour) / mid (next 2 days) /
   long (next month). Each horizon needs a signed % + probability + named
   source. Conflicting horizons across timeframes are fine — name the
   conflict instead of smoothing it.

## Output — Telegram analysis ping

Send via `send_telegram_update`. Markdown-safe (no stray backticks; the
server auto-falls-back to plain text on parse error, but cleaner to avoid
the round-trip). No hard length cap — budget ~1500–2500 chars depending on
how many names you highlight.

Cap yourself at **3 highlighted symbols per ping**. If you're seeing more,
pick the strongest 3 and reference the rest in the trailing `Also watching:`
line.

### Format

```
🛰 *<agent>* · <HOURLY @ HH:MM ET | OCAP on <SYM> @ HH:MM ET (triggers: <rule_a>, <rule_b>)>
Regime: <🟢/🟡/🔴> <one-clause read>  ·  Mike said: <agree | disagree because X>
TL;DR: <SYM +conv why> | <SYM +conv why> | <SYM +conv why>

— <SYM1> · direction=<long|short-via-<INV>|flat> · conviction=<X.XX> · framework=<T+F+Q | T+Q | F-only | ...>
  Inputs this run: SMA20=<x>, SMA50=<x>, RSI14=<x>, ATR14=<x>, BBANDS_pos=<x>, <model_name>@v<N>=<signal|expected_pct>
  Next hour:   <±X.X%> p=<0.XX> — <intraday tape / model_name@vN / news_id>
  Next 2 days: <±X.X%> p=<0.XX> — <catalyst id / setup name>
  Next month:  <±X.X%> p=<0.XX> — <macro / cycle thesis>
  Why this beats alternatives: <one or two sentences arguing this trade vs the next-best in the universe>
  News: "<headline>" — <source>, <ts>   OR   No fresh news (searched <HH:MM ET>, last hit: <date or "none">)

— <SYM2> · ...  (same block)

— <SYM3> · ...  (same block)

Also watching: <SYM4 reason> · <SYM5 reason>     (optional, ≤2)
```

### Source ID conventions

Each multi-horizon line cites at least one source. Use these IDs verbatim
so the user can grep transcripts later:

- `model_name@vN` — quant model output (e.g. `garch_drift@v3`)
- `news:<id>` — a news article ID returned by `get_news`
- `tech:<indicator>=<value>` — a specific indicator read (e.g. `tech:RSI14=28`)
- `cat:<symbol-or-event>:<date>` — calendar catalyst from `get_upcoming_catalysts`
- `peer:<agent>:<symbol>` — cross-agent context lifted via `get_thread_posts`

### Material change — FLIP prefix

If your sector view flipped net-long ↔ net-short this hour (vs your last
`get_my_active_views` slate), prefix the entire ping with `⚠️ FLIP:` so it
stands out at a glance. Only use this for net-direction flips, not for
single-name swaps within an unchanged net stance.

### Empty / no-edge fallback

If you genuinely have nothing meaningful to publish (everyone flat, no
catalyst, RSI cluster mid-range), still send ONE line so the user knows you
ran and chose to stand aside rather than failing silently:

```
🛰 *<agent>* @ HH:MM ET — No edge. Universe scanned: N names, all between RSI 30 and 70, no catalyst within 5 sessions. Standing aside.
```

Substitute `<agent>` with your name (atlas, fab, fabless, rex, maya, vera,
trump, iron, volt, energy, commodity). Adjust the "Universe scanned" suffix
to match what you actually checked — but the leading line and "Standing
aside." closer are required so the user's grep finds it consistently.

## Code-adjustment block (append IF you edited a model this run)

When a `*-review` does an inline fix or a `*-model-tune` ships a change,
send a SECOND `send_telegram_update` call immediately after the analysis
ping (keeps the two visually separable, lets the user reply to one without
scrolling past the other):

```
🔧 *<agent>* code adjustment
File: agents/<agent>/models/<filename>.py
Change: <one sentence: what behavior changed>
Why: <hypothesis / bug / calibration miss this fixes>
MODEL_VERSION: <old> → <new>
Verified: re-ran compute on <SYM>, output was <signal=… direction=… conv=…>
```

If you scrapped a file, set `Change: scrapped` and skip `MODEL_VERSION` /
`Verified`. If you added a new file, set `Change: new model` and quote the
first verified compute result.

## Forbidden

- Quoting a number you didn't pull this run.
- Saying "RSI looks elevated" without the actual value.
- Citing "news flow" without a `get_news` call this run.
- Publishing a multi-horizon forecast with identical probabilities across
  short/mid/long (signals you didn't think about the timeframes separately).
- Editing a model file without sending the code-adjustment block.

## Python / MCP harness — what you can use

Confirmed available across `*-review` and `*-model-tune` skills:

- **`*-review` skills** — `Read` + `Edit` on `agents/<self>/models/*.py`.
  Use this for inline fixes ≤30 lines per `[BROKEN MODEL DECISION RULE]`.
  No `Write`, no `Bash` here — larger changes get deferred to `*-model-tune`.
- **`*-model-tune` skills** — `Read` + `Edit` + `Write` + `Bash`. Full Python
  authoring: new files, scrap files, `python -c "..."` sanity checks,
  re-run `compute_all_models` mid-edit, etc.
- **Quant primitives** — `compute_technicals`, `compute_all_models`,
  `compute_custom_indicator`, `get_bars` (5-min, 1-day, weekly).
- **Telegram out** — `send_telegram_update` (text), `send_telegram_chart`
  (image + caption).
- **State writes** — `submit_conviction_view`, `submit_forecast_batch`,
  `submit_conviction_from_model`, `record_thesis`, `update_thesis_status`,
  `raise_tool_gap`, `propose_strategic_change`.

If you need a tool that isn't in your skill's `allowed-tools` block,
`raise_tool_gap(...)` — don't silently work around it.
