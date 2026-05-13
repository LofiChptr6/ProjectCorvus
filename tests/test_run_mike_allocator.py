"""Unit tests for scripts/run_mike_allocator.py.

Pure-function tests run fully offline. Async tests mock mcp_server / vLLM /
db.store at module boundaries — no DB, no IBKR, no real LLM, no Telegram.
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


# Override the conftest's session-scoped autouse _init_schema fixture: this
# module mocks every DB boundary, so we don't need a live DB. The conftest
# fixture currently trips a pre-existing UniqueViolationError on init_db when
# running against a production-seeded `agent_forecast` table — unrelated to
# this PR but blocks our pure-function tests from collecting.
@pytest_asyncio.fixture(scope="session", autouse=True)
async def _init_schema():
    yield


def _load_runner():
    path = _REPO_ROOT / "scripts" / "run_mike_allocator.py"
    spec = importlib.util.spec_from_file_location("run_mike_allocator", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def runner():
    return _load_runner()


# ─────────────────────────────────────────────────────────────────────────────
# _template_why — pure deterministic fallback
# ─────────────────────────────────────────────────────────────────────────────

def test_template_why_no_orders(runner):
    out = runner._template_why({"orders_placed": [], "target_weights": {}, "cash_weight": 0.0})
    assert "no orders placed" in out


def test_template_why_longs_only_below_cash_threshold(runner):
    result = {
        "orders_placed": [{"symbol": "X", "side": "BUY", "qty": 1, "result": {"status": "submitted"}}],
        "target_weights": {"NVDA": 0.15, "AAPL": 0.10},
        "cash_weight": 0.02,
    }
    out = runner._template_why(result)
    assert "2 longs" in out
    assert "hedges" not in out
    assert "cash" not in out


def test_template_why_longs_shorts_and_cash(runner):
    result = {
        "orders_placed": [{"symbol": "X", "side": "BUY", "qty": 1, "result": {"status": "submitted"}}],
        "target_weights": {"NVDA": 0.15, "AAPL": 0.10, "SQQQ": -0.06},
        "cash_weight": 0.12,
    }
    out = runner._template_why(result)
    assert "2 longs" in out and "1 hedges" in out and "12% cash" in out


def test_template_why_shorts_only_high_cash(runner):
    result = {
        "orders_placed": [{"symbol": "X", "side": "BUY", "qty": 1, "result": {"status": "submitted"}}],
        "target_weights": {"SQQQ": -0.06, "SH": -0.05},
        "cash_weight": 0.45,
    }
    out = runner._template_why(result)
    assert "2 hedges" in out and "longs" not in out and "45% cash" in out


# ─────────────────────────────────────────────────────────────────────────────
# _format_telegram — pure deterministic formatter
# ─────────────────────────────────────────────────────────────────────────────

def test_format_telegram_basic_layout(runner):
    result = {
        "target_weights": {"NVDA": 0.15, "AAPL": 0.10, "SQQQ": -0.06, "GLD": 0.08, "AMZN": 0.04},
        "cash_weight": 0.12,
        "orders_placed": [
            {"symbol": "NVDA", "side": "BUY", "qty": 12, "result": {"status": "submitted"}},
            {"symbol": "SQQQ", "side": "BUY", "qty": 50, "result": {"status": "submitted"}},
            {"symbol": "X",    "side": "BUY", "qty": 1,  "result": {"status": "blocked"}},
        ],
        "cap_dropped": [{"symbol": "Z"}],
        "pending_user_review": [],
        "pending_inverse_approvals": [],
    }
    out = runner._format_telegram(result, n_symbols=5, n_agents=4, why="testing reasons")
    assert "🧭 *Allocator @ " in out
    assert "5 sym / 4 agents" in out
    # Top 3 by |w|: NVDA(0.15), AAPL(0.10), GLD(0.08)
    assert "NVDA +15.0%" in out
    assert "AAPL +10.0%" in out
    assert "GLD +8.0%" in out
    assert "cash 12%" in out
    assert "*Placed:* 2 of 3" in out  # 2 submitted, 1 blocked
    assert "1 capped" in out
    assert "*Why:* testing reasons" in out


def test_format_telegram_omits_cash_below_threshold(runner):
    result = {
        "target_weights": {"NVDA": 0.15},
        "cash_weight": 0.04,
        "orders_placed": [],
        "cap_dropped": [], "pending_user_review": [], "pending_inverse_approvals": [],
    }
    out = runner._format_telegram(result, 1, 1, "x")
    assert "cash" not in out


def test_format_telegram_signs_negative_weights(runner):
    result = {
        "target_weights": {"SQQQ": -0.20, "SH": -0.10},
        "cash_weight": 0.0,
        "orders_placed": [], "cap_dropped": [], "pending_user_review": [], "pending_inverse_approvals": [],
    }
    out = runner._format_telegram(result, 2, 1, "x")
    assert "SQQQ -20.0%" in out
    assert "SH -10.0%" in out


def test_format_telegram_truncated_to_1000(runner):
    result = {
        "target_weights": {"A": 0.1}, "cash_weight": 0.0,
        "orders_placed": [], "cap_dropped": [], "pending_user_review": [], "pending_inverse_approvals": [],
    }
    out = runner._format_telegram(result, 1, 1, "X" * 5000)
    assert len(out) <= 1000


def test_format_telegram_errored_orders_counted(runner):
    result = {
        "target_weights": {"A": 0.1}, "cash_weight": 0.0,
        "orders_placed": [
            {"symbol": "A", "side": "BUY", "qty": 1, "result": {"status": "submitted"}},
            {"symbol": "B", "side": "BUY", "qty": 1, "error": "RuntimeError: boom"},
            {"symbol": "C", "side": "BUY", "qty": 1, "result": {"status": "error"}},
        ],
        "cap_dropped": [], "pending_user_review": [], "pending_inverse_approvals": [],
    }
    out = runner._format_telegram(result, 1, 1, "x")
    assert "*Placed:* 1 of 3" in out
    assert "2 errored" in out


def test_format_telegram_empty_targets(runner):
    result = {
        "target_weights": {}, "cash_weight": 0.0,
        "orders_placed": [], "cap_dropped": [], "pending_user_review": [], "pending_inverse_approvals": [],
    }
    out = runner._format_telegram(result, 0, 0, "no signal")
    assert "(no targets)" in out


def test_format_telegram_all_extras_joined(runner):
    result = {
        "target_weights": {"A": 0.1}, "cash_weight": 0.0,
        "orders_placed": [
            {"symbol": "A", "side": "BUY", "qty": 1, "result": {"status": "submitted"}},
        ],
        "cap_dropped": [{"symbol": "B"}, {"symbol": "C"}],
        "pending_user_review": [{"symbol": "D"}],
        "pending_inverse_approvals": [{"id": "x"}],
    }
    out = runner._format_telegram(result, 1, 1, "x")
    assert "2 capped" in out
    assert "1 awaiting approval" in out
    assert "1 inverse-ETF gated" in out


# ─────────────────────────────────────────────────────────────────────────────
# _guard_skip — async, mocked at mcp_server + module-level datetime
# ─────────────────────────────────────────────────────────────────────────────

class _FakeDatetime:
    """Replace runner.datetime so we can pin AZ hour deterministically."""
    def __init__(self, hour: int):
        self._hour = hour
    def now(self, tz=None):
        return SimpleNamespace(hour=self._hour, strftime=lambda fmt: "12:34 ET")


async def _fake_market_json(is_open: bool, next_open: str = "2026-05-13T09:30:00-04:00"):
    return json.dumps({"is_open": is_open, "next_open_et": next_open})


async def _fake_kill_json(global_kill=False, mike_kill=False):
    return json.dumps({"global_kill": global_kill, "per_agent": {"mike": mike_kill}})


def _patch_mcp_guards(monkeypatch, *, market_open=True, global_kill=False, mike_kill=False):
    import mcp_server
    monkeypatch.setattr(mcp_server, "get_market_status",
                        lambda: _fake_market_json(market_open))
    monkeypatch.setattr(mcp_server, "get_kill_switch_status",
                        lambda: _fake_kill_json(global_kill=global_kill, mike_kill=mike_kill))


async def test_guard_quiet_window_late_evening(runner, monkeypatch):
    monkeypatch.setattr(runner, "datetime", _FakeDatetime(22))
    _patch_mcp_guards(monkeypatch)
    reason = await runner._guard_skip()
    assert reason is not None
    assert "AZ quiet window" in reason
    assert "hour=22" in reason


async def test_guard_quiet_window_early_morning(runner, monkeypatch):
    monkeypatch.setattr(runner, "datetime", _FakeDatetime(4))
    _patch_mcp_guards(monkeypatch)
    reason = await runner._guard_skip()
    assert reason is not None and "AZ quiet window" in reason


async def test_guard_quiet_window_boundary_5_passes(runner, monkeypatch):
    """Hour 5 is the start of the *active* window — guard must NOT fire."""
    monkeypatch.setattr(runner, "datetime", _FakeDatetime(5))
    _patch_mcp_guards(monkeypatch, market_open=True)
    reason = await runner._guard_skip()
    assert reason is None


async def test_guard_market_closed(runner, monkeypatch):
    monkeypatch.setattr(runner, "datetime", _FakeDatetime(10))
    _patch_mcp_guards(monkeypatch, market_open=False)
    reason = await runner._guard_skip()
    assert reason is not None and "market closed" in reason


async def test_guard_global_kill(runner, monkeypatch):
    monkeypatch.setattr(runner, "datetime", _FakeDatetime(10))
    _patch_mcp_guards(monkeypatch, global_kill=True)
    reason = await runner._guard_skip()
    assert reason is not None and "global kill" in reason


async def test_guard_mike_kill(runner, monkeypatch):
    monkeypatch.setattr(runner, "datetime", _FakeDatetime(10))
    _patch_mcp_guards(monkeypatch, mike_kill=True)
    reason = await runner._guard_skip()
    assert reason is not None and "mike kill switch" in reason


async def test_guard_all_clear_returns_none(runner, monkeypatch):
    monkeypatch.setattr(runner, "datetime", _FakeDatetime(10))
    _patch_mcp_guards(monkeypatch)
    reason = await runner._guard_skip()
    assert reason is None


# ─────────────────────────────────────────────────────────────────────────────
# _llm_why_sentence — mock pipelines.llm_client.make_client
# ─────────────────────────────────────────────────────────────────────────────

class _FakeCompletions:
    def __init__(self, content: str):
        self._content = content
        self.last_kwargs: dict | None = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))])


def _install_fake_llm(monkeypatch, content: str):
    fake_completions = _FakeCompletions(content)
    fake_llm_client = SimpleNamespace(
        client=SimpleNamespace(chat=SimpleNamespace(completions=fake_completions)),
        model="fake-model", base_url="http://fake", session_id="fake-sid",
    )
    import pipelines.llm_client as llm_mod
    monkeypatch.setattr(llm_mod, "make_client", lambda **_: fake_llm_client)
    return fake_completions


@pytest.fixture
def sample_result():
    return {
        "target_weights": {"NVDA": 0.15, "AAPL": 0.10, "SQQQ": -0.06},
        "contributing_views": {
            "NVDA": [{"agent": "fabless", "weight": 0.6}],
            "AAPL": [{"agent": "rex", "weight": 0.5}],
            "SQQQ": [{"agent": "atlas", "weight": 0.4}],
        },
        "orders_placed": [
            {"symbol": "NVDA", "side": "BUY", "qty": 10, "result": {"status": "submitted"}},
        ],
        "cash_weight": 0.07,
        "cash_contributors": [{"agent": "atlas", "weight": 0.3}],
        "skipped_views": [],
    }


async def test_llm_why_returns_clean_sentence(runner, monkeypatch, sample_result):
    _install_fake_llm(monkeypatch, "Leaning into oversold semis with one inverse hedge.")
    out = await runner._llm_why_sentence(sample_result)
    assert out == "Leaning into oversold semis with one inverse hedge."


async def test_llm_why_strips_think_block(runner, monkeypatch, sample_result):
    _install_fake_llm(monkeypatch, "<think> long thinking </think>The desk is leaning into oversold semis.")
    out = await runner._llm_why_sentence(sample_result)
    assert out == "The desk is leaning into oversold semis."
    assert "<think>" not in out


async def test_llm_why_strips_wrapping_quotes(runner, monkeypatch, sample_result):
    _install_fake_llm(monkeypatch, '"Quoted reply."')
    out = await runner._llm_why_sentence(sample_result)
    assert out == "Quoted reply."


async def test_llm_why_truncates_to_220(runner, monkeypatch, sample_result):
    _install_fake_llm(monkeypatch, "a" * 1000)
    out = await runner._llm_why_sentence(sample_result)
    assert len(out) <= 220


async def test_llm_why_empty_falls_back_to_placeholder(runner, monkeypatch, sample_result):
    _install_fake_llm(monkeypatch, "   \n  ")
    out = await runner._llm_why_sentence(sample_result)
    assert out == "desk rebalanced"


async def test_llm_why_collapses_newlines(runner, monkeypatch, sample_result):
    _install_fake_llm(monkeypatch, "line one\nline two\nline three")
    out = await runner._llm_why_sentence(sample_result)
    assert "\n" not in out
    assert "line one" in out and "line three" in out


async def test_llm_why_prompt_includes_top_weights_and_no_think(runner, monkeypatch, sample_result):
    completions = _install_fake_llm(monkeypatch, "ok")
    await runner._llm_why_sentence(sample_result)
    user_msg = next(m["content"] for m in completions.last_kwargs["messages"] if m["role"] == "user")
    assert "NVDA +15.0%" in user_msg
    assert "fabless" in user_msg
    assert "/no_think" in user_msg


# ─────────────────────────────────────────────────────────────────────────────
# main() — end-to-end with every boundary mocked
# ─────────────────────────────────────────────────────────────────────────────

def _install_db_convictions(monkeypatch, rows: list[dict]):
    async def _fake():
        return rows
    import db.store as store
    monkeypatch.setattr(store, "get_active_convictions", _fake)


def _install_telegram_capture(monkeypatch) -> list[str]:
    captured: list[str] = []
    async def _capture(text, **kwargs):
        captured.append(text)
        return {"ok": True}
    import approval.telegram as tg
    monkeypatch.setattr(tg, "send_message", _capture)
    return captured


async def test_main_guard_skip_returns_2(runner, monkeypatch):
    monkeypatch.setattr(runner, "datetime", _FakeDatetime(23))
    rc = await runner.main()
    assert rc == 2


async def test_main_insufficient_views_returns_0_with_telegram(runner, monkeypatch):
    monkeypatch.setattr(runner, "datetime", _FakeDatetime(10))
    _patch_mcp_guards(monkeypatch)
    _install_db_convictions(monkeypatch, [
        {"agent_name": "vera", "symbol": "MRNA"},
        {"agent_name": "vera", "symbol": "PFE"},
    ])
    sent = _install_telegram_capture(monkeypatch)

    rc = await runner.main()
    assert rc == 0
    assert len(sent) == 1
    assert "insufficient views" in sent[0].lower()
    assert "2 sym" in sent[0] and "1 agent" in sent[0]


async def test_main_happy_path_sends_telegram_with_why(runner, monkeypatch):
    monkeypatch.setattr(runner, "datetime", _FakeDatetime(10))
    _patch_mcp_guards(monkeypatch)
    _install_db_convictions(monkeypatch, [
        {"agent_name": "vera", "symbol": "MRNA"},
        {"agent_name": "rex", "symbol": "AAPL"},
        {"agent_name": "fabless", "symbol": "NVDA"},
    ])

    fake_result = {
        "decision_id": 42, "nav": 50000.0,
        "target_weights": {"NVDA": 0.15, "AAPL": 0.10, "MRNA": 0.08},
        "contributing_views": {"NVDA": [{"agent": "fabless", "weight": 0.6}]},
        "orders_placed": [
            {"symbol": "NVDA", "side": "BUY", "qty": 12, "result": {"status": "submitted"}},
        ],
        "cap_dropped": [], "pending_user_review": [], "pending_inverse_approvals": [],
        "cash_weight": 0.05, "cash_contributors": [], "skipped_views": [],
    }
    import mcp_server
    async def _fake_rebalance(**_): return json.dumps(fake_result)
    monkeypatch.setattr(mcp_server, "rebalance_desk", _fake_rebalance)

    # Force the template fallback so we don't depend on the LLM
    async def _llm_fails(_): raise RuntimeError("vLLM down")
    monkeypatch.setattr(runner, "_llm_why_sentence", _llm_fails)

    sent = _install_telegram_capture(monkeypatch)

    rc = await runner.main()
    assert rc == 0
    assert len(sent) == 1
    body = sent[0]
    assert "Allocator @" in body
    assert "3 sym / 3 agents" in body
    assert "NVDA +15.0%" in body
    assert "*Placed:* 1 of 1" in body
    # Template fallback ran (LLM raised)
    assert "*Why:*" in body
    assert "rebalancing into" in body


async def test_main_rebalance_error_returns_1(runner, monkeypatch):
    monkeypatch.setattr(runner, "datetime", _FakeDatetime(10))
    _patch_mcp_guards(monkeypatch)
    _install_db_convictions(monkeypatch, [
        {"agent_name": "vera", "symbol": "MRNA"},
        {"agent_name": "rex", "symbol": "AAPL"},
        {"agent_name": "fabless", "symbol": "NVDA"},
    ])
    import mcp_server
    async def _bad_rebalance(**_): return json.dumps({"error": "rate_limited"})
    monkeypatch.setattr(mcp_server, "rebalance_desk", _bad_rebalance)

    sent = _install_telegram_capture(monkeypatch)
    rc = await runner.main()
    assert rc == 1
    assert any("rate_limited" in t for t in sent)
