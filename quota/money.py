"""Money helpers. Money is integer cents; rates are integer basis points."""


def fmt_usd(cents: int) -> str:
    """1800000 -> "$18,000.00"; negatives -> "-$18,000.00"."""
    sign = "-" if cents < 0 else ""
    dollars, rem = divmod(abs(cents), 100)
    return f"{sign}${dollars:,}.{rem:02d}"


def to_bps(rate) -> int:
    """Plan YAML decimal rate -> basis points (0.10 -> 1000)."""
    return round(rate * 10000)


def apply_bps(base_cents: int, rate_bps: int) -> int:
    return base_cents * rate_bps // 10000


def fmt_pct(bps: int) -> str:
    """14000 -> "140.00%"."""
    sign = "-" if bps < 0 else ""
    whole, frac = divmod(abs(bps), 100)
    return f"{sign}{whole}.{frac:02d}%"
