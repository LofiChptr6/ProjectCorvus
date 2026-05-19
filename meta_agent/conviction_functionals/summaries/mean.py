def compute(xs: list[float], ps: list[float]) -> float:
    return sum(x * p for x, p in zip(xs, ps))
