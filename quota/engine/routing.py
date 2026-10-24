"""Approval routing (pure)."""

from quota import config

OWNERS = {"plan_exception": "marcus.lee", "journal_entry": "priya.nair", "payroll_file": "priya.nair"}
ESCALATION = {"marcus.lee": "priya.nair", "priya.nair": None}


def owner(kind) -> str:
    return OWNERS[kind]


def limit(principal, kind) -> int | None:
    """Approval limit in cents from config; None = unlimited."""
    return config.principals(with_seed=True).get(principal, {}).get("limit")


def next_approver(kind, amount_cents, after: str) -> str | None:
    """First principal up the escalation chain from `after` whose limit covers the amount."""
    candidate = ESCALATION.get(after)
    while candidate is not None:
        cap = limit(candidate, kind)
        if cap is None or amount_cents <= cap:
            return candidate
        candidate = ESCALATION.get(candidate)
    return None
