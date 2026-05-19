"""Unit tests for scripts/run_hourly_review.py.

Pure-function tests are offline. Async paths mock every mcp_server /
db.store / vLLM / Telegram boundary.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# Override the conftest's session-scoped autouse _init_schema fixture.
@pytest_asyncio.fixture(scope="session", autouse=True)
async def _init_schema():
    yield


def _load_runner():
    path = _REPO_ROOT / "scripts" / "run_hourly_review.py"
    spec = importlib.util.spec_from_file_location("run_hourly_review", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def runner():
    return _load_runner()


# ─────────────────────────────────────────────────────────────────────────────
# _quiet_window_active — pure
# ─────────────────────────────────────────────────────────────────────────────

class _FakeDatetime:
    def __init__(self, hour: int):
        self._hour = hour
    def now(self, tz=None):
        return SimpleNamespace(hour=self._hour, strftime=lambda fmt: "12:34 ET")


@pytest.mark.parametrize("hour,expected", [
    (22, True), (23, True), (0, True), (4, True),
    (5, False), (10, False), (15, False), (21, False),
])
def test_quiet_window_hours(runner, monkeypatch, hour, expected):
    monkeypatch.setattr(runner, "datetime", _FakeDatetime(hour))
    assert runner._quiet_window_active() is expected


# ─────────────────────────────────────────────────────────────────────────────
# _market_mode_emoji — pure
# ─────────────────────────────────────────────────────────────────────────────

def test_market_mode_emoji_open_full(runner):
    assert runner._market_mode_emoji({"is_open": True, "is_half_day": False}) == "🟢"


def test_market_mode_emoji_half_day(runner):
    assert runner._market_mode_emoji({"is_open": True, "is_half_day": True}) == "🟡"


def test_market_mode_emoji_closed(runner):
    assert runner._market_mode_emoji({"is_open": False}) == "⚫"


# ─────────────────────────────────────────────────────────────────────────────
# _summarize_fills — pure
# ─────────────────────────────────────────────────────────────────────────────

def test_summarize_fills_empty(runner):
    n, summary = runner._summarize_fills([])
    assert n == 0 and "no new positions" in summary


def test_summarize_fills_only_buys(runner):
    fills = [
        {"action": "BOT", "symbol": "NVDA"},
        {"action": "BUY", "symbol": "AAPL"},
    ]
    n, summary = runner._summarize_fills(fills)
    assert n == 2
    assert "added" in summary
    assert "AAPL" in summary and "NVDA" in summary
    assert "trimmed" not in summary


def test_summarize_fills_only_sells(runner):
    fills = [
        {"action": "SLD", "symbol": "VOO"},
        {"action": "SELL", "symbol": "QQQ"},
    ]
    n, summary = runner._summarize_fills(fills)
    assert n == 2
    assert "trimmed" in summary
    assert "VOO" in summary and "QQQ" in summary
    assert "added" not in summary


def test_summarize_fills_mixed(runner):
    fills = [
        {"action": "BOT", "symbol": "LMT"},
        {"action": "BOT", "symbol": "LRCX"},
        {"action": "SLD", "symbol": "VOO"},
    ]
    n, summary = runner._summarize_fills(fills)
    assert n == 3
    assert "added" in summary and "trimmed" in summary
    assert "LMT" in summary and "LRCX" in summary and "VOO" in summary


def test_summarize_fills_dedupes_same_symbol(runner):
    fills = [{"action": "BOT", "symbol": "NVDA"} for _ in range(5)]
    n, summary = runner._summarize_fills(fills)
    assert n == 5  # raw count
    assert summary.count("NVDA") == 1  # dedupe in label


def test_summarize_fills_ignores_blank_symbol(runner):
    fills = [{"action": "BOT", "symbol": ""}, {"action": "BOT", "symbol": "NVDA"}]
    n, summary = runner._summarize_fills(fills)
    assert "NVDA" in summary


# ─────────────────────────────────────────────────────────────────────────────
# _short_heartbeat_if_quiet — pure
# ─────────────────────────────────────────────────────────────────────────────

def test_short_heartbeat_format(runner):
    out = runner._short_heartbeat_if_quiet(
        market={"is_open": True, "is_half_day": False},
        balances={"nav": 50000, "cash": 5000},
        positions=[{"symbol": "A", "quantity": 10}, {"symbol": "B", "quantity": 0}, {"symbol": "C", "quantity": 5}],
    )
    assert "quiet hour" in out
    assert "NAV $50,000" in out
    assert "cash 10%" in out
    assert "2 positions" in out  # only 2 with nonzero qty


def test_short_heartbeat_zero_nav(runner):
    """Don't divide by zero on degenerate NAV."""
    out = runner._short_heartbeat_if_quiet(
        market={"is_open": False},
        balances={"nav": 0, "cash": 0},
        positions=[],
    )
    assert "cash 0%" in out


# ─────────────────────────────────────────────────────────────────────────────
# _template_watch — pure
# ─────────────────────────────────────────────────────────────────────────────

def test_template_watch_kill_active(runner):
    out = runner._template_watch([], [], {"global_kill": True, "per_agent": {"mike": False}})
    assert "kill switch" in out


def test_template_watch_mike_kill_only(runner):
    out = runner._template_watch([], [], {"global_kill": False, "per_agent": {"mike": True}})
    assert "kill switch" in out


def test_template_watch_quiet(runner):
    out = runner._template_watch([], [], {"global_kill": False, "per_agent": {"mike": False}})
    assert "nothing concerning" in out


def test_template_watch_with_proposals(runner):
    out = runner._template_watch([], [{"id": "x"}, {"id": "y"}], {"global_kill": False, "per_agent": {}})
    assert "2 approval-gated" in out


def test_template_watch_with_fills_no_proposals(runner):
    out = runner._template_watch(
        [{"action": "BOT", "symbol": "A"}, {"action": "BOT", "symbol": "B"}],
        [], {"global_kill": False, "per_agent": {}},
    )
    assert "2 fill" in out


# ─────────────────────────────────────────────────────────────────────────────
# _compose_heartbeat — pure
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def full_state():
    return {
        "market": {"is_open": True, "is_half_day": False},
        "ks": {"global_kill": False, "per_agent": {"mike": False}},
        "balances": {"nav": 50000, "cash": 5000},
        "positions": [{"symbol": "A", "quantity": 10}, {"symbol": "B", "quantity": 0}, {"symbol": "C", "quantity": 5}],
        "open_orders": [{"symbol": "X", "id": 1}, {"symbol": "Y", "id": 2}],
        "pnl_windows": {"desk": {"today": {"pnl_usd": 123.45}, "wtd": {"pnl_usd": 200.0}}},
        "proposals": [{"id": "p1"}],
        "fills": [{"action": "BOT", "symbol": "NVDA", "order_id": 100},
                  {"action": "SLD", "symbol": "VOO", "order_id": 101}],
    }


def test_compose_heartbeat_full_layout(runner, full_state):
    out = runner._compose_heartbeat(
        full_state["market"], full_state["ks"], full_state["balances"],
        full_state["positions"], full_state["open_orders"], full_state["pnl_windows"],
        full_state["proposals"], full_state["fills"], "monitor NVDA earnings AH",
    )
    assert "*Heartbeat" in out
    assert "2 fills" in out
    assert "added NVDA" in out and "trimmed VOO" in out
    assert "2 working orders" in out
    assert "1 approval-gated" in out
    assert "kill=ok" in out
    assert "day P&L=$123" in out
    assert "monitor NVDA earnings AH" in out
    assert "NAV $50,000" in out and "cash 10%" in out and "2 positions" in out


def test_compose_heartbeat_truncated_to_700(runner, full_state):
    out = runner._compose_heartbeat(
        full_state["market"], full_state["ks"], full_state["balances"],
        full_state["positions"], full_state["open_orders"], full_state["pnl_windows"],
        full_state["proposals"], full_state["fills"], "X" * 2000,
    )
    assert len(out) <= 700


def test_compose_heartbeat_kill_active(runner, full_state):
    full_state["ks"]["global_kill"] = True
    out = runner._compose_heartbeat(
        full_state["market"], full_state["ks"], full_state["balances"],
        full_state["positions"], full_state["open_orders"], full_state["pnl_windows"],
        full_state["proposals"], full_state["fills"], "watch",
    )
    assert "kill=active" in out


def test_compose_heartbeat_uses_windowed_day_pnl_not_cumulative(runner, full_state):
    """Regression: day P&L must read desk.today.pnl_usd, not a cumulative-since-
    inception number. Pre-fix the heartbeat read totals.total_pnl from
    get_pnl_summary, which was cumulative."""
    pnl_windows = {"desk": {"today": {"pnl_usd": -73.30}, "wtd": {"pnl_usd": -55.62}}}
    out = runner._compose_heartbeat(
        full_state["market"], full_state["ks"], full_state["balances"],
        full_state["positions"], full_state["open_orders"], pnl_windows,
        full_state["proposals"], full_state["fills"], "w",
    )
    assert "day P&L=$-73" in out
    assert "$-650" not in out and "$-745" not in out  # the broken cumulative values


def test_compose_heartbeat_day_pnl_none_renders_na(runner, full_state):
    """If the today window has no snapshot (e.g. agent_state empty), the
    heartbeat must render n/a — not silently substitute 0."""
    pnl_windows = {"desk": {"today": {"pnl_usd": None}, "wtd": {"pnl_usd": None}}}
    out = runner._compose_heartbeat(
        full_state["market"], full_state["ks"], full_state["balances"],
        full_state["positions"], full_state["open_orders"], pnl_windows,
        full_state["proposals"], full_state["fills"], "w",
    )
    assert "day P&L=n/a" in out


def test_compose_heartbeat_empty_pnl_windows_renders_na(runner, full_state):
    """If the pnl_windows fetch failed entirely (empty dict), still render n/a
    without crashing."""
    out = runner._compose_heartbeat(
        full_state["market"], full_state["ks"], full_state["balances"],
        full_state["positions"], full_state["open_orders"], {},
        full_state["proposals"], full_state["fills"], "w",
    )
    assert "day P&L=n/a" in out


# ─────────────────────────────────────────────────────────────────────────────
# _llm_watch_sentence — mock the OpenAI client
# ─────────────────────────────────────────────────────────────────────────────

class _FakeCompletions:
    def __init__(self, content: str):
        self._content = content
        self.last_kwargs: dict | None = None
    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))])


def _install_fake_llm(monkeypatch, content: str):
    fake = SimpleNamespace(
        client=SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions(content))),
        model="fake", base_url="http://fake", session_id="sid",
    )
    import pipelines.llm_client as llm_mod
    monkeypatch.setattr(llm_mod, "make_client", lambda **_: fake)
    return fake.client.chat.completions


async def test_llm_watch_clean_sentence(runner, monkeypatch, full_state):
    _install_fake_llm(monkeypatch, "Monitor NVDA earnings AH.")
    out = await runner._llm_watch_sentence(
        full_state["market"], full_state["fills"], full_state["pnl_windows"],
        full_state["proposals"], full_state["ks"],
    )
    assert out == "Monitor NVDA earnings AH."


async def test_llm_watch_strips_think(runner, monkeypatch, full_state):
    _install_fake_llm(monkeypatch, "<think>thinking</think>Watch the close.")
    out = await runner._llm_watch_sentence(
        full_state["market"], full_state["fills"], full_state["pnl_windows"],
        full_state["proposals"], full_state["ks"],
    )
    assert out == "Watch the close."


async def test_llm_watch_empty_falls_back(runner, monkeypatch, full_state):
    _install_fake_llm(monkeypatch, "   ")
    out = await runner._llm_watch_sentence(
        full_state["market"], full_state["fills"], full_state["pnl_windows"],
        full_state["proposals"], full_state["ks"],
    )
    assert out == "nothing concerning"


async def test_llm_watch_truncated_to_200(runner, monkeypatch, full_state):
    _install_fake_llm(monkeypatch, "a" * 1000)
    out = await runner._llm_watch_sentence(
        full_state["market"], full_state["fills"], full_state["pnl_windows"],
        full_state["proposals"], full_state["ks"],
    )
    assert len(out) <= 200


async def test_llm_watch_prompt_uses_no_think(runner, monkeypatch, full_state):
    completions = _install_fake_llm(monkeypatch, "ok")
    await runner._llm_watch_sentence(
        full_state["market"], full_state["fills"], full_state["pnl_windows"],
        full_state["proposals"], full_state["ks"],
    )
    user_msg = next(m["content"] for m in completions.last_kwargs["messages"] if m["role"] == "user")
    assert "/no_think" in user_msg


# ─────────────────────────────────────────────────────────────────────────────
# main() — end-to-end with every boundary mocked
# ─────────────────────────────────────────────────────────────────────────────

def _install_state_mocks(monkeypatch, **overrides):
    """Install fakes for every MCP / DB call inside _load_state."""
    import mcp_server
    from db import store

    defaults = {
        "market": {"is_open": True, "is_half_day": False, "mode": "trading"},
        "kill_switch": {"global_kill": False, "per_agent": {"mike": False}},
        "balances": {"nav": 50000, "cash": 5000},
        "positions": {"positions": []},
        "open_orders": {"orders": []},
        "pnl_windows": {"desk": {"today": {"pnl_usd": 0.0}, "wtd": {"pnl_usd": 0.0}}},
        "proposals": {"pending": []},
        "fills": [],
    }
    state = {**defaults, **overrides}

    async def _market(): return json.dumps(state["market"])
    async def _ks(): return json.dumps(state["kill_switch"])
    async def _bal(): return json.dumps(state["balances"])
    async def _pos(): return json.dumps(state["positions"])
    async def _oo(): return json.dumps(state["open_orders"])
    async def _pnlw(**_): return json.dumps(state["pnl_windows"])
    async def _props(): return json.dumps(state["proposals"])
    async def _fills(**_): return state["fills"]

    monkeypatch.setattr(mcp_server, "get_market_status", _market)
    monkeypatch.setattr(mcp_server, "get_kill_switch_status", _ks)
    monkeypatch.setattr(mcp_server, "get_balances", _bal)
    monkeypatch.setattr(mcp_server, "get_positions", _pos)
    monkeypatch.setattr(mcp_server, "get_open_orders", _oo)
    monkeypatch.setattr(mcp_server, "get_agent_pnl_windows", _pnlw)
    monkeypatch.setattr(mcp_server, "list_pending_proposals", _props)
    monkeypatch.setattr(store, "get_fills_window", _fills)


def _install_telegram_capture(monkeypatch) -> list[str]:
    captured: list[str] = []
    async def _capture(text, **kwargs):
        captured.append(text)
        return {"ok": True}
    import approval.telegram as tg
    monkeypatch.setattr(tg, "send_message", _capture)
    return captured


async def test_main_quiet_window_silent_exit(runner, monkeypatch):
    """Quiet window → no Telegram, exit 0."""
    monkeypatch.setattr(runner, "_quiet_window_active", lambda: True)
    sent = _install_telegram_capture(monkeypatch)
    rc = await runner.main()
    assert rc == 0
    assert sent == []  # no Telegram during quiet window


async def test_main_no_activity_sends_short_heartbeat(runner, monkeypatch):
    monkeypatch.setattr(runner, "_quiet_window_active", lambda: False)
    _install_state_mocks(monkeypatch)
    sent = _install_telegram_capture(monkeypatch)

    rc = await runner.main()
    assert rc == 0
    assert len(sent) == 1
    assert "quiet hour" in sent[0]
    assert "0 positions" in sent[0]


async def test_main_with_activity_sends_full_heartbeat(runner, monkeypatch):
    monkeypatch.setattr(runner, "_quiet_window_active", lambda: False)
    _install_state_mocks(monkeypatch,
        fills=[{"action": "BOT", "symbol": "NVDA", "order_id": 100}],
        open_orders={"orders": [{"id": 1, "symbol": "X"}]},
        positions={"positions": [{"symbol": "NVDA", "quantity": 10}]},
    )
    async def _llm_fail(*a, **kw): raise RuntimeError("vLLM down")
    monkeypatch.setattr(runner, "_llm_watch_sentence", _llm_fail)
    sent = _install_telegram_capture(monkeypatch)

    rc = await runner.main()
    assert rc == 0
    assert len(sent) == 1
    body = sent[0]
    assert "*Heartbeat" in body
    assert "1 fills" in body
    assert "added NVDA" in body
    assert "1 working orders" in body
    assert "*Watch:*" in body  # template fallback fired


async def test_main_kill_active_triggers_long_heartbeat(runner, monkeypatch):
    """Kill switch counts as 'activity' for the long-form heartbeat."""
    monkeypatch.setattr(runner, "_quiet_window_active", lambda: False)
    _install_state_mocks(monkeypatch,
        kill_switch={"global_kill": True, "per_agent": {"mike": False}},
    )
    async def _llm_fail(*a, **kw): raise RuntimeError("vLLM down")
    monkeypatch.setattr(runner, "_llm_watch_sentence", _llm_fail)
    sent = _install_telegram_capture(monkeypatch)

    rc = await runner.main()
    assert rc == 0
    assert "kill=active" in sent[0]
    assert "quiet hour" not in sent[0]  # Long-form, not short


async def test_main_telegram_failure_returns_1(runner, monkeypatch):
    monkeypatch.setattr(runner, "_quiet_window_active", lambda: False)
    _install_state_mocks(monkeypatch)

    async def _bad_send(*a, **kw): raise RuntimeError("telegram down")
    import approval.telegram as tg
    monkeypatch.setattr(tg, "send_message", _bad_send)

    rc = await runner.main()
    assert rc == 1
