"""Amortization schedules (pure)."""

from quota.clock import add_periods

EXPENSE_IMMEDIATELY_MAX_MONTHS = 12   # ASC 340-40 practical expedient: term <= 12 months is expensed


def capitalize(term_months: int) -> bool:
    return term_months > EXPENSE_IMMEDIATELY_MAX_MONTHS


def build_schedule(total_cents: int, months: int, start_period: str) -> list[tuple[str, int]]:
    """Floor each month; remainder to the last month. 1_800_000 over 36 from 2026-04 -> 36 x 50_000."""
    each = total_cents // months
    schedule = [(add_periods(start_period, i), each) for i in range(months)]
    period, cents = schedule[-1]
    schedule[-1] = (period, cents + total_cents - each * months)
    return schedule


def amount_for(schedule, period) -> int:
    return sum(cents for p, cents in schedule if p == period)


def amortized_through(schedule, period) -> int:
    """Sum of months <= period."""
    return sum(cents for p, cents in schedule if p <= period)


def remaining_after(schedule, period) -> int:
    return sum(cents for p, cents in schedule if p > period)
