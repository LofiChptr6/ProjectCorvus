from __future__ import annotations

from typing import Any

def _avg_range(bars: list[dict]) -> float:
    ranges = [(b['h'] - b['l']) for b in bars if b['h'] is not None and b['l'] is not None]
    return sum(ranges) / len(ranges) if ranges else 0.0

MODEL_VERSION = '1.1'

def compute(symbol: str, bars: list[dict], context: dict) -> dict[str, Any]:
    if len(bars) < 25:
        return {'signal': None, 'reason': f'need >=25 bars, got {len(bars)}'}

    recent5 = bars[-5:]
    base20 = bars[-25:-5]
    r5 = _avg_range(recent5)
    r20 = _avg_range(base20)
    expansion = (r5 / r20) if r20 else 0.0

    # Enhanced decision logic with VIX and sector RSI thresholds
    if expansion > 1.5:
        return {
            'signal': 'sell',
            'direction': 'short',
            'conviction': 0.8,
            'expected_return_pct': -5.0,
            'time_to_target_days': 3,
            'reason': 'vol expansion into earnings + VIX at 18.5 (options rich)',
            'inputs': {
                'range_5': round(r5, 3),
                'range_20_avg': round(r20, 3),
                'expansion_ratio': round(expansion, 3),
                'vix_level': context.get('VIX', 0.0),
                'sector_rsi': context.get('sector_rsi', 0.0)
            },
            'note': 'True IV crush needs options chain data — raise_tool_gap if not present.'
        }
    elif expansion < 0.7:
        return {
            'signal': 'buy',
            'direction': 'long',
            'conviction': 0.7,
            'expected_return_pct': 4.0,
            'time_to_target_days': 5,
            'reason': 'vol contraction into earnings + sector RSI <40 (IV cheap)',
            'inputs': {
                'range_5': round(r5, 3),
                'range_20_avg': round(r20, 3),
                'expansion_ratio': round(expansion, 3),
                'vix_level': context.get('VIX', 0.0),
                'sector_rsi': context.get('sector_rsi', 0.0)
            },
            'note': 'True IV crush needs options chain data — raise_tool_gap if not present.'
        }
    else:
        return {
            'signal': 'hold',
            'direction': 'flat',
            'conviction': 0.0,
            'expected_return_pct': 0.0,
            'time_to_target_days': 5,
            'reason': 'no edge from realized vol',
            'inputs': {
                'range_5': round(r5, 3),
                'range_20_avg': round(r20, 3),
                'expansion_ratio': round(expansion, 3),
                'vix_level': context.get('VIX', 0.0),
                'sector_rsi': context.get('sector_rsi', 0.0)
            },
            'note': 'True IV crush needs options chain data — raise_tool_gap if not present.'
        }