"""Chart of accounts, principals, capabilities, approval limits, materiality."""

ACCOUNTS = {
    "1000": ("Cash", "asset"),
    "1200": ("Accounts receivable", "asset"),
    "1290": ("Due from employees", "asset"),
    "1350": ("Deferred commissions", "asset"),      # single account in Phase 1; current/noncurrent split is cut
    "2150": ("Commissions payable", "liability"),
    "2400": ("Deferred revenue", "liability"),
    "4000": ("Subscription revenue", "revenue"),
    "6110": ("Commission expense, amortization", "expense"),
    "6115": ("Commission expense, not capitalized", "expense"),
}

HUMAN_CAPABILITIES = frozenset({"message:send", "read:own"})


def _human(name, title, capabilities, limit=None):
    return {"kind": "human", "name": name, "title": title,
            "capabilities": frozenset(capabilities) | HUMAN_CAPABILITIES, "limit": limit}


PRINCIPALS = {
    # claim:event (Phase 2) lets an actor mark an event it picked up from the queue as handled.
    "seat:commissions_analyst": {
        "kind": "agent seat", "limit": None,
        "capabilities": frozenset({"read:*", "draft:commission_line", "draft:approval_request",
                                   "draft:message", "draft:payroll_file", "record:decision", "claim:event"}),
    },
    "seat:deferred_accountant": {
        "kind": "agent seat", "limit": None,
        "capabilities": frozenset({"read:*", "draft:journal_entry", "record:decision", "claim:event"}),
    },
    "system:billing": {"kind": "simulated ERP", "limit": None,
                       "capabilities": frozenset({"post:source_entry", "claim:event"})},
    "system": {"kind": "simulator", "limit": None, "capabilities": frozenset({"emit:event"})},
    "priya.nair": _human("Priya Nair", "Controller",
                         {"approve:plan_exception", "approve:journal_entry", "approve:payroll_file",
                          "lock:period"}, limit=None),
    "marcus.lee": _human("Marcus Lee", "VP Sales Ops", {"approve:plan_exception"}, limit=500000),
    "dana.ortiz": _human("Dana Ortiz", "Payroll manager", {"release:payroll_file"}),
    "morgan.diaz": _human("Morgan Diaz", "Sales manager", set()),
    "jordan.kim": _human("Jordan Kim", "AE", {"dispute:own_lines", "message:send"}),
}

# Seeding writes facts directly; `seed` exists in tests and the demo only.
SEED_PRINCIPAL = {"seed": {"kind": "seed", "limit": None, "capabilities": frozenset({"*"})}}

MATERIALITY_CENTS = 50000          # $500
MATERIALITY_BPS_OF_PAYOUT = 200    # 2%
COMPANY_TZ = "UTC"                 # keep UTC everywhere for the hackathon


def principals(with_seed: bool = False) -> dict:
    return {**PRINCIPALS, **SEED_PRINCIPAL} if with_seed else dict(PRINCIPALS)


def has_capability(principal_map: dict, principal: str, capability: str) -> bool:
    caps = principal_map.get(principal, {}).get("capabilities", frozenset())
    if "*" in caps or capability in caps:
        return True
    prefix = capability.split(":", 1)[0]
    return f"{prefix}:*" in caps


def display_name(principal: str) -> str:
    return PRINCIPALS.get(principal, {}).get("name", principal)


def title(principal: str) -> str:
    return PRINCIPALS.get(principal, {}).get("title", "")
