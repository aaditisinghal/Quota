"""Chart of accounts, principals, capabilities, approval limits, materiality."""

ACCOUNTS = {
    "1000": ("Cash", "asset"),
    "1200": ("Accounts receivable", "asset"),
    "1290": ("Commission advances recoverable", "asset"),
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
        "capabilities": frozenset({"read:*", "draft:journal_entry", "draft:message", "record:decision", "claim:event"}),
    },
    "system:billing": {"kind": "simulated ERP", "limit": None,
                       "capabilities": frozenset({"post:source_entry", "claim:event"})},
    "system": {"kind": "simulator", "limit": None, "capabilities": frozenset({"emit:event"})},
    # Phase 3: the simulated CRM / e-signature log / HR records / plan store, and the CRM's automation.
    "system:records": {"kind": "simulated CRM, e-sign, HR, plans", "limit": None,
                       "capabilities": frozenset({"write:fact", "claim:event"})},
    "salesops.bot": {"kind": "bot", "name": "Sales Ops bot", "title": "Automated colleague", "limit": None,
                     "capabilities": frozenset()},
    "priya.nair": _human("Priya Nair", "Controller",
                         {"approve:plan_exception", "approve:journal_entry", "approve:payroll_file",
                          "approve:commitment_review", "lock:period"}, limit=None),
    "marcus.lee": _human("Marcus Lee", "VP Sales Ops", {"approve:plan_exception"}, limit=500000),
    # SPEC "Who it asks": the Controller's backup when she has not answered in 2 business days. The CFO decides
    # only approvals routed to her (INV5_WRONG_APPROVER otherwise).
    "grace.liu": _human("Grace Liu", "CFO",
                        {"approve:plan_exception", "approve:journal_entry", "approve:payroll_file",
                         "approve:commitment_review"}, limit=None),
    "dana.ortiz": _human("Dana Ortiz", "Payroll manager", {"release:payroll_file"}),
    "morgan.diaz": _human("Morgan Diaz", "Sales manager", set()),
    "jordan.kim": _human("Jordan Kim", "AE", {"dispute:own_lines", "message:send"}),
}

# Seeding writes facts directly; `seed` exists in tests and the demo only.
SEED_PRINCIPAL = {"seed": {"kind": "seed", "limit": None, "capabilities": frozenset({"*"})}}

MATERIALITY_CENTS = 50000          # $500
MATERIALITY_BPS_OF_PAYOUT = 200    # 2%
COMPANY_TZ = "America/Los_Angeles" # quarters and periods close in company time (R1)

# Approver behavior (Phase 2)
DIGEST_TIME = "09:00:00Z"          # one daily digest per approver
ESCALATE_AFTER_BUSINESS_DAYS = 2   # then routing.ESCALATION backup; never auto-approve
PAYROLL_DAY = 15                   # payroll for period YYYY-MM releases on YYYY-MM-15
PAYROLL_BLOCKING_DAYS = 2          # approvals blocking a payroll this close go out immediately
PROPOSE_PLAN_CHANGE_AFTER = 2      # the same kind of exception on this many deals -> propose a signed plan change
COLLECTION_TOLERANCE_BPS = 9800    # a payment of 98% or more of the invoice counts as collected (R5)


def principals(with_seed: bool = False) -> dict:
    return {**PRINCIPALS, **SEED_PRINCIPAL} if with_seed else dict(PRINCIPALS)


def rep_principal(name: str, title: str = "AE") -> dict:
    return _human(name, title, {"dispute:own_lines"})


def register_principals(extra: dict) -> None:
    """Generated worlds add their reps (names and capabilities) to the directory."""
    PRINCIPALS.update({k: v for k, v in extra.items() if k not in PRINCIPALS})


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
