"""Unit tests for the queue worker's quant_distribution_compute dispatcher.
Exercises the in-process handler without spawning subprocesses or touching
the database — the underlying compute_conviction_payload is mocked."""
from __future__ import annotations

import asyncio
import importlib
from unittest.mock import AsyncMock, patch


def _worker():
    return importlib.import_module("scripts.run_queue_worker")


def test_quant_dispatch_ok_returns_exit_zero_and_rollup():
    W = _worker()
    job = {"id": 1, "agent_name": "atlas", "job_type": "quant_distribution_compute"}
    payload = {"model_name": "hmm_regime_mix", "symbol": "SPY"}
    fake = AsyncMock(return_value={
        "status": "ok",
        "model_version": "0.1.0",
        "payload": {
            "direction": "long", "conviction": 0.4,
            "forecast_run_id": "abc-123", "functional_name": "expected_return",
            "expected_return_pct": 1.2, "time_to_target_days": 5,
            "stop_pct": None, "model_inputs": {},
        },
    })
    with patch("meta_agent.conviction_from_model.compute_conviction_payload", fake):
        exit_code, rollup = asyncio.run(W._dispatch_quant_distribution(job, payload))
    assert exit_code == 0
    assert rollup["status"] == "ok"
    assert rollup["forecast_run_id"] == "abc-123"
    assert rollup["functional_name"] == "expected_return"
    fake.assert_awaited_once_with("atlas", "hmm_regime_mix", "SPY")


def test_quant_dispatch_skipped_is_exit_zero():
    W = _worker()
    job = {"id": 2, "agent_name": "atlas", "job_type": "quant_distribution_compute"}
    payload = {"model_name": "lgbm_bin_classifier", "symbol": "QQQ"}
    fake = AsyncMock(return_value={
        "status": "skipped", "reason": "insufficient bars", "model_version": "0.1.0",
    })
    with patch("meta_agent.conviction_from_model.compute_conviction_payload", fake):
        exit_code, rollup = asyncio.run(W._dispatch_quant_distribution(job, payload))
    assert exit_code == 0
    assert rollup["status"] == "skipped"
    assert rollup["reason"] == "insufficient bars"


def test_quant_dispatch_error_is_exit_nonzero():
    W = _worker()
    job = {"id": 3, "agent_name": "atlas", "job_type": "quant_distribution_compute"}
    payload = {"model_name": "hmm_regime_mix", "symbol": "SPY"}
    fake = AsyncMock(return_value={
        "status": "error", "error": "model crashed: ValueError: bad input",
    })
    with patch("meta_agent.conviction_from_model.compute_conviction_payload", fake):
        exit_code, rollup = asyncio.run(W._dispatch_quant_distribution(job, payload))
    assert exit_code == 1
    assert rollup["status"] == "error"
    assert "ValueError" in rollup["error"]


def test_quant_dispatch_missing_payload_fields_returns_error():
    W = _worker()
    job = {"id": 4, "agent_name": "atlas", "job_type": "quant_distribution_compute"}
    payload = {"model_name": "hmm_regime_mix"}  # no symbol
    exit_code, rollup = asyncio.run(W._dispatch_quant_distribution(job, payload))
    assert exit_code == 1
    assert "missing" in rollup["error"]


def test_quant_dispatch_catches_compute_crashes():
    W = _worker()
    job = {"id": 5, "agent_name": "atlas", "job_type": "quant_distribution_compute"}
    payload = {"model_name": "hmm_regime_mix", "symbol": "SPY"}
    crashing = AsyncMock(side_effect=RuntimeError("hmm explosion"))
    with patch("meta_agent.conviction_from_model.compute_conviction_payload", crashing):
        exit_code, rollup = asyncio.run(W._dispatch_quant_distribution(job, payload))
    assert exit_code == 1
    assert "RuntimeError" in rollup["error"]
    assert "hmm explosion" in rollup["error"]
