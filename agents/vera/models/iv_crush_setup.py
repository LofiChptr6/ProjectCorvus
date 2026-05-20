from __future__ import annotations

from typing import Any

MODEL_VERSION = '1.3'
BAR_FREQUENCY = '1d'
MIN_BARS = 25
LOOKBACK_DAYS = 60
EXTRA_SYMBOLS = ['VIXY', 'XLV']


def _rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    avg_g = gains / n
    avg_l = losses / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - 100 / (1 + rs), 2)


def _avg_range(bars: list[dict]) -> float:
    rs = [b['h'] - b['l'] for b in bars if b['h'] is not None and b['l'] is not None]
    return sum(rs) / len(rs) if rs else 0.0


def compute(symbol: str, bars: list[dict], context: dict) -> dict[str, Any]:
    if len(bars) < MIN_BARS:
        return {
            'signal': None, 'direction': None, 'likelihood': 0.0,
            'expected_return_pct': 0.0, 'time_to_target_days': 0,
            'inputs': {},
            'reason': f'need >={MIN_BARS} bars, got {len(bars)}',
        }

    extras = context.get('extra_bars') or {}
    vix_bars = extras.get('VIXY') or []
    xlv_bars = extras.get('XLV') or []
    if len(vix_bars) < 30 or len(xlv_bars) < 15:
        return {
            'signal': None, 'direction': None, 'likelihood': 0.0,
            'expected_return_pct': 0.0, 'time_to_target_days': 0,
            'inputs': {},
            'reason': f'extras incomplete (VIXY={len(vix_bars)}, XLV={len(xlv_bars)}; need VIXY>=30 and XLV>=15)',
        }

    r5 = _avg_range(bars[-5:])
    r20 = _avg_range(bars[-25:-5])
    expansion = r5 / r20 if r20 else 0.0

    vix_closes = [float(b['c']) for b in vix_bars]
    vix_now = vix_closes[-1]
    vix_60d_mean = sum(vix_closes[-60:]) / min(60, len(vix_closes))
    vix_ratio = vix_now / vix_60d_mean if vix_60d_mean else 0.0

    xlv_closes = [b['c'] for b in xlv_bars]
    xlv_rsi = _rsi(xlv_closes)
    if xlv_rsi is None:
        return {
            'signal': None, 'direction': None, 'likelihood': 0.0,
            'expected_return_pct': 0.0, 'time_to_target_days': 0,
            'inputs': {},
            'reason': 'xlv_rsi computation failed',
        }

    inputs = {
        'expansion_ratio': round(expansion, 3),
        'vixy_ratio_vs_60d': round(vix_ratio, 3),
        'xlv_rsi': float(xlv_rsi),
    }

    if expansion > 1.5 and vix_ratio > 1.1:
        return {
            'signal': round(expansion, 3),
            'direction': 'flat',
            'likelihood': 0.0,
            'expected_return_pct': 0.0,
            'time_to_target_days': 3,
            'inputs': inputs,
            'interpretation': 'vol expansion + VIXY above 60d mean (fade-IV; bearish — emit flat)',
        }
    if expansion < 0.7 and xlv_rsi < 40:
        return {
            'signal': round(-expansion, 3),
            'direction': 'long',
            'likelihood': round(min(0.4 + (40 - xlv_rsi) / 100, 0.9), 3),
            'expected_return_pct': 4.0,
            'time_to_target_days': 5,
            'inputs': inputs,
            'interpretation': 'vol contraction + XLV oversold (IV cheap; lean directional long)',
        }
    # Add a new bearish case: sector ETF strength breaking + high IV
    if expansion > 1.3 and vix_ratio > 1.15 and xlv_rsi > 60:
        return {
            'signal': round(expansion, 3),
            'direction': 'flat',
            'likelihood': 0.0,
            'expected_return_pct': 0.0,
            'time_to_target_days': 2,
            'inputs': inputs,
            'interpretation': 'vol expansion + VIXY elevated + XLV ETF overbought (IV overpricing + sector fatigue; fade-earnings overreaction)',
        }
    return {
        'signal': round(expansion, 3),
        'direction': 'flat',
        'likelihood': 0.0,
        'expected_return_pct': 0.0,
        'time_to_target_days': 0,
        'inputs': inputs,
        'interpretation': 'no clear earnings-driven setup in current environment',
    }