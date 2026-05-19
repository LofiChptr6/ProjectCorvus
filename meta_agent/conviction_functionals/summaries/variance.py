def compute(xs: list[float], ps: list[float]) -> float:
    mu = sum(x * p for x, p in zip(xs, ps))
    return sum(p * (x - mu) ** 2 for x, p in zip(xs, ps))
