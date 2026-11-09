"""Integer reference model; input times and amounts are never floating point."""

from .core import WEEK

YEAR = 365 * 86400
REDUCTION = 1189207115002721024


def weight(slope, end, week):
    return slope * max(end - week, 0)


def partition(start, stop, rate, epoch, cap, weights):
    """Return conservative earning segments, aggregate bounds, and final rate state."""
    if not 0 <= start <= stop or epoch < start or cap < 0:
        raise ValueError("Invalid integration interval")
    rows, upper, lower = [], 0, 0
    while start < stop:
        if start == epoch:
            rate = rate * 10**18 // REDUCTION
            epoch += YEAR
        week = start // WEEK * WEEK
        end = min(stop, week + WEEK, epoch)
        k = rate * weights[week]
        u = k * (end - start) // 10**18
        l = (k // 10**18) * (end - start)
        rows.append((week, min(l, max(0, cap - upper))))
        upper, lower, start = upper + u, lower + l, end
    return rows, min(cap, lower), min(cap, upper), rate, epoch
