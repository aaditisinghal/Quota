"""The printed timeline line (IMPLEMENTATION.md section 10) and the small formatters every actor shares."""

from quota.money import fmt_usd


def format_line(clock_now, actor, kind, text) -> str:
    head = f"[{clock_now}] {actor:<26} {kind:<9} "
    first, *rest = text.split("\n")
    return "\n".join([head + first] + [" " * len(head) + line for line in rest])


def signed(cents) -> str:
    return f"+{fmt_usd(cents)}" if cents >= 0 else fmt_usd(cents)


def entry_text(lines) -> str:
    """"Dr 1200 / Cr 2400 $120,000.00", or per-line amounts when they differ."""
    sides = [("Dr " if l["debit_cents"] else "Cr ") + l["account"] for l in lines]
    amounts = [l["debit_cents"] or l["credit_cents"] for l in lines]
    if len(set(amounts)) == 1:
        return f"{' / '.join(sides)} {fmt_usd(amounts[0])}"
    return " / ".join(f"{s} {fmt_usd(a)}" for s, a in zip(sides, amounts))
