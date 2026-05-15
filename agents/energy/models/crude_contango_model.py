MODEL_VERSION = "1.0"

def compute(symbol, bars, context):
    # Assume symbol is a crude ETF or futures proxy (e.g., USO, CL, etc.)
    # Check backwardation/contango regime in futures
    # This model returns bullish when crude futures are in backwardation (demand > supply), bearish otherwise
    if 'backwardation' in context:
        backwardation = context['backwardation']
    else:
        backwardation = False

    inputs = {
        'symbol': symbol,
        'backwardation': backwardation
    }

    if backwardation:
        return {
            'direction': 'long',
            'conviction': 0.6,
            'expected_return_pct': 3.0,
            'time_to_target_days': 14,
            'inputs': inputs
        }
    else:
        return {
            'direction': 'flat',
            'conviction': 0.0,
            'expected_return_pct': 0.0,
            'time_to_target_days': 0,
            'inputs': inputs
        }