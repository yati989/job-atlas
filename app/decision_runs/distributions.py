from math import floor, ceil

def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * percent / 100
    low, high = floor(pos), ceil(pos)
    return ordered[low] if low == high else ordered[low] + (ordered[high] - ordered[low]) * (pos - low)

def salary_distribution(values: list[float | None]) -> dict:
    known = [v for v in values if v is not None]
    return {"known_n": len(known), "unknown_n": len(values) - len(known),
            **{f"p{p}": percentile(known, p) for p in (10, 25, 50, 75, 90)},
            "mean": sum(known) / len(known) if known else None}
