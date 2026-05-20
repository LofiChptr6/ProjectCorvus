"""LightGBM multi-class softmax over fixed return bins.

Trains an N-bin classifier on bar-derived features → "next-bar return falls
in bin k". Inference returns the softmax probabilities directly as the
forecast distribution — no Gaussian parametric assumption.

Features per bar (windowed lookback):
  - last_return, last_log_return
  - 5-bar / 10-bar / 20-bar mean log-return (trend at three scales)
  - 5-bar / 20-bar realized vol (std of log returns)
  - 20-bar RSI (momentum oscillator, 0–1 scaled)
  - last_close / 20-bar MA (price stretch vs trend)

Trade-offs (per the plan):
  - Trained fresh per call on the most recent bars. Cheap on small data
    (~50ms for 200 bars × 8 features × 11 classes) but should eventually
    move to a nightly-trained-and-cached model via `model_tune`.
  - Marked queue-only — do NOT call from an inline review path. Dispatch
    via the `quant_distribution_compute` agent_job. The LightGBM training
    call burns ~50–300ms; not acceptable in the per-symbol hourly loop.

Horizon: 1d (one bar ahead at BAR_FREQUENCY=1d).
"""
from __future__ import annotations

import math
import warnings
from datetime import datetime, timezone
from typing import Any

import numpy as np

try:
    import lightgbm as lgb
    _LGB_AVAILABLE = True
except ImportError:
    _LGB_AVAILABLE = False

MODEL_VERSION = "0.1.0"
BAR_FREQUENCY = "1d"
LOOKBACK_DAYS = 200
MIN_BARS = 80
EXTRA_SYMBOLS: list[str] = []

_HORIZON = ("1d", 1)
_N_BINS = 11
_BIN_LO_PCT = -3.0
_BIN_HI_PCT = 3.0
_P_FLOOR = 1.0e-4
_TRAIN_WINDOW = 150     # bars used for training (must be < len(bars) - feature_window)
_FEATURE_WINDOW = 20    # bars needed to compute the largest feature


def _no_signal(reason: str) -> dict[str, Any]:
    return {
        "signal": None,
        "direction": None,
        "likelihood": 0.0,
        "expected_return_pct": 0.0,
        "time_to_target_days": 0,
        "inputs": {},
        "reason": reason,
    }


def _features_at(closes: np.ndarray, i: int) -> list[float] | None:
    """Compute feature vector at bar index i. Returns None if not enough
    history to fill all features (i.e. i < _FEATURE_WINDOW)."""
    if i < _FEATURE_WINDOW or closes[i - 1] <= 0 or closes[i] <= 0:
        return None
    last_return = (closes[i] - closes[i - 1]) / closes[i - 1]
    last_log = math.log(closes[i] / closes[i - 1])

    def window_log_returns(n: int) -> list[float]:
        out = []
        for k in range(i - n + 1, i + 1):
            if k <= 0 or closes[k - 1] <= 0 or closes[k] <= 0:
                continue
            out.append(math.log(closes[k] / closes[k - 1]))
        return out

    r5 = window_log_returns(5)
    r10 = window_log_returns(10)
    r20 = window_log_returns(20)
    mean5 = sum(r5) / max(len(r5), 1)
    mean10 = sum(r10) / max(len(r10), 1)
    mean20 = sum(r20) / max(len(r20), 1)
    vol5 = (sum((x - mean5) ** 2 for x in r5) / max(len(r5) - 1, 1)) ** 0.5 if r5 else 0.0
    vol20 = (sum((x - mean20) ** 2 for x in r20) / max(len(r20) - 1, 1)) ** 0.5 if r20 else 0.0

    # RSI-14
    gains = [max(r, 0) for r in r20[-14:]]
    losses = [max(-r, 0) for r in r20[-14:]]
    avg_gain = sum(gains) / max(len(gains), 1)
    avg_loss = sum(losses) / max(len(losses), 1)
    if avg_loss == 0:
        rsi = 1.0 if avg_gain > 0 else 0.5
    else:
        rs = avg_gain / avg_loss
        rsi = 1.0 - 1.0 / (1.0 + rs)

    # Price stretch vs 20-bar MA
    ma20 = sum(closes[i - 19 : i + 1]) / 20
    stretch = (closes[i] - ma20) / ma20 if ma20 > 0 else 0.0

    return [last_return, last_log, mean5, mean10, mean20, vol5, vol20, rsi, stretch]


def _label_bin(next_return_pct: float) -> int:
    """Map next-bar return (percent) to bin index 0..N-1."""
    if next_return_pct <= _BIN_LO_PCT:
        return 0
    if next_return_pct >= _BIN_HI_PCT:
        return _N_BINS - 1
    spacing = (_BIN_HI_PCT - _BIN_LO_PCT) / (_N_BINS - 1)
    idx = int(round((next_return_pct - _BIN_LO_PCT) / spacing))
    return max(0, min(_N_BINS - 1, idx))


def compute(symbol: str, bars: list[dict], context: dict) -> dict[str, Any]:
    if not _LGB_AVAILABLE:
        return _no_signal("lightgbm not installed; install via requirements.txt")
    if len(bars) < MIN_BARS:
        return _no_signal(f"need >={MIN_BARS} bars, got {len(bars)}")
    raw_closes = [float(b["c"]) for b in bars if b.get("c") is not None and float(b["c"]) > 0]
    if len(raw_closes) < MIN_BARS:
        return _no_signal(f"insufficient positive closes, got {len(raw_closes)}")
    closes = np.array(raw_closes, dtype=float)

    # Build training set on the rolling window (drop the most recent bar; we
    # use its features for inference and don't have its outcome).
    X_train: list[list[float]] = []
    y_train: list[int] = []
    train_start = max(_FEATURE_WINDOW, len(closes) - _TRAIN_WINDOW)
    for i in range(train_start, len(closes) - 1):
        feats = _features_at(closes, i)
        if feats is None:
            continue
        next_ret_pct = (closes[i + 1] - closes[i]) / closes[i] * 100.0
        y_train.append(_label_bin(next_ret_pct))
        X_train.append(feats)
    if len(X_train) < 40:
        return _no_signal(f"insufficient train samples: {len(X_train)}")

    X_train_arr = np.array(X_train, dtype=float)
    y_train_arr = np.array(y_train, dtype=int)

    # LightGBM softmax classifier. Quiet settings + small forest for speed.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        clf = lgb.LGBMClassifier(
            objective="multiclass",
            num_class=_N_BINS,
            n_estimators=80,
            max_depth=4,
            num_leaves=15,
            learning_rate=0.08,
            min_data_in_leaf=5,
            verbose=-1,
            random_state=42,
        )
        try:
            clf.fit(X_train_arr, y_train_arr)
        except Exception as exc:
            return _no_signal(f"lgbm fit failed: {type(exc).__name__}: {exc}")

    last_idx = len(closes) - 1
    feats_now = _features_at(closes, last_idx)
    if feats_now is None:
        return _no_signal("cannot compute inference features")
    with warnings.catch_warnings():
        # LightGBM warns about missing feature names on raw ndarray inputs
        # (fit took ndarray, predict also takes ndarray — no real mismatch).
        warnings.simplefilter("ignore")
        proba = clf.predict_proba(np.array([feats_now], dtype=float))[0]

    # LightGBM might have learned fewer classes than _N_BINS if the training
    # set never visited some bins; reshape to full _N_BINS with zeros.
    full_proba = np.full(_N_BINS, 0.0, dtype=float)
    for k, cls in enumerate(clf.classes_):
        full_proba[int(cls)] = float(proba[k])

    spacing = (_BIN_HI_PCT - _BIN_LO_PCT) / (_N_BINS - 1)
    centers = [round(_BIN_LO_PCT + i * spacing, 8) for i in range(_N_BINS)]
    probs = [max(p, _P_FLOOR) for p in full_proba.tolist()]
    s = sum(probs)
    probs = [p / s for p in probs]

    distribution = {
        "anchor_price": round(float(closes[-1]), 4),
        "anchor_ts": datetime.now(timezone.utc).isoformat(),
        "axis": "return_pct",
        "horizon": _HORIZON[0],
        "bins": [{"x": c, "p": round(p, 6)} for c, p in zip(centers, probs)],
        "model": "lgbm_bin_classifier",
        "model_version": MODEL_VERSION,
    }

    mu_pct = sum(x * p for x, p in zip(centers, probs))
    if abs(mu_pct) < 0.1:
        direction = "flat"
        e_return = 0.0
        ttd = 0
    else:
        direction = "long" if mu_pct > 0 else "short"
        e_return = round(mu_pct, 3)
        ttd = _HORIZON[1]

    inputs = {
        "lgbm_n_train":  len(X_train),
        "lgbm_n_classes_fit": int(len(clf.classes_)),
        "feat_last_return":   round(float(feats_now[0]), 6),
        "feat_mean20":        round(float(feats_now[4]), 8),
        "feat_vol20":         round(float(feats_now[6]), 8),
        "feat_rsi14":         round(float(feats_now[7]), 4),
        "feat_stretch_vs_ma20": round(float(feats_now[8]), 6),
        "last_close":         round(float(closes[-1]), 4),
    }

    return {
        "signal": round(mu_pct, 3),
        "direction": direction,
        "likelihood": min(abs(mu_pct) / 3.0, 1.0) if direction != "flat" else 0.0,
        "expected_return_pct": e_return,
        "time_to_target_days": ttd,
        "stop_pct": None,
        "inputs": inputs,
        "distributions": [distribution],
        "interpretation": (
            f"lgbm_bin_classifier: 1d E[r]={mu_pct:.2f}% n_train={len(X_train)}"
        ),
    }
