# agents/commodity/models/gold_real_yield_model.py
MODEL_VERSION = "1.0"

def compute(symbol, bars, context):
    # Simple model: gold is a real yield inverse.
    # This model will output a long/short signal based on 10Y TIPS yield change.
    # For simplicity, use 10Y TIPS yield from context.
    
    # Example: if TIPS yield rises → gold gets bearish → model says short (long GLL)
    
    # Placeholder logic for demo — replace with real data linkage.
    # Assume we have access to TIPS yield via context['real_yield_10y']
    
    real_yield_change = context.get('real_yield_10y_change', 0)
    
    # If TIPS yield rises, gold gets bearish
    if real_yield_change > 0:
        return {
            'direction': 'short',
            'conviction': 0.6,
            'expected_return_pct': -3.0,
            'time_to_target_days': 10,
            'inputs': {'real_yield_10y_change': real_yield_change}
        }
    elif real_yield_change < 0:
        return {
            'direction': 'long',
            'conviction': 0.6,
            'expected_return_pct': +3.0,
            'time_to_target_days': 10,
            'inputs': {'real_yield_10y_change': real_yield_change}
        }
    else:
        return {
            'direction': 'flat',
            'conviction': 0.0,
            'expected_return_pct': 0.0,
            'time_to_target_days': 0,
            'inputs': {'real_yield_10y_change': real_yield_change}
        }