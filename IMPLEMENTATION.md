# Quota — Implementation Spec (for Claude Code)

This file is the build contract. SPEC.md explains *why*; this file says *exactly what to build*. When they disagree, this file wins.

## 0. How to work

- Build phases in order. Inside a phase, do steps in order. After each step: run `pytest -q`, show output, `git commit`, continue to the next step without waiting.
- Never ask questions. If something is unclear, choose the simplest option, append one line to `ASSUMPTIONS.md`, keep going.
- Do not add features not listed here. Do not add a UI before Phase 4.
- Phase 1 uses **no LLM calls**. Phase 2 adds them behind a `--scripted` flag so the demo never depends on a model.

## 1. Conventions (apply everywhere)

| Thing | Rule |
| --- | --- |
| Money | Integer **cents** (`int`). Never floats. Display with `fmt_usd(cents) -> "$18,000.00"`. |
| Rates | Integer **basis points** (`1000` = 10%). Plan YAML uses decimals; convert on load with `round(x * 10000)`. |
| Integer math | `amount = base_cents * rate_bps // 10000`. Amortization: floor per month, remainder added to the **last** month. |
| Time | ISO-8601 UTC strings with `Z` (`2026-04-02T17:14:00Z`). Dates `YYYY-MM-DD`. Periods `YYYY-MM`. Quarters `2026Q1`. |
| Booking quarter | Quarter of the UTC date of the booking timestamp. |
| Clock | All writes take `known_at = clock.now()`. The clock only moves forward; moving it backward raises. |
| IDs | Zero-padded sequential with prefix: `JE-0001`, `D-0001`, `A-0001`, `CL-0001`, `P-0001`, `R-0001` (refusal), `EV-0001`. Facts use readable ids: `acme`, `jordan.kim`. |
| Append-only | No `UPDATE` or `DELETE` on business tables, ever. A change is a new row (`version + 1`, or `corrects_id`). Status changes are new rows in a `*_status` table or a new version row. |
| Python | 3.11, stdlib `sqlite3`, `dataclasses`, `pyyaml`, `pytest`. FastAPI only from Phase 4. |

## 2. File tree (create exactly this in Phase 1)

```
pyproject.toml
README.md                      # placeholder; filled in Phase 3
ASSUMPTIONS.md
plans/jordan.kim/AE-2026-H1-v3.yaml
quota/
  __init__.py
  __main__.py                  # CLI entry: python -m quota ...
  config.py                    # accounts, principals, capabilities, limits, materiality
  money.py                     # fmt_usd, bps helpers
  clock.py                     # SimClock
  ledger/
    __init__.py
    schema.sql
    db.py                      # connect(path, readonly), init_db
    hashing.py                 # sha256 helpers, chain append/verify
    write_path.py              # WritePath: the ONLY writer
    invariants.py              # check functions, return list[Violation]
    queries.py                 # as_of(), trial_balance(), lines_for(), etc.
  engine/
    __init__.py
    plans.py                   # Plan dataclass + load_plan(path)
    ranking.py                 # source-of-truth resolution with rule ids
    calculator.py              # commission math (pure)
    amortization.py            # schedules (pure)
    entries.py                 # journal entry builders (pure)
    disputes.py                # dispute policy (pure, deterministic)
    routing.py                 # approval routing (pure)
  sim/
    __init__.py
    seed_acme.py               # seeds facts for the demo world
    demo_acme.py               # the scripted timeline (section 9)
tests/
  conftest.py                  # fresh in-memory DB + clock fixture
  test_calculator.py
  test_amortization.py
  test_write_path.py
  test_routing.py
  test_chain.py
  test_replay.py
  test_demo_acme.py
```

`pyproject.toml`: package `quota`, deps `pyyaml`, `pytest`. Add `fastapi`, `uvicorn` in Phase 4, `anthropic` in Phase 2.

## 3. Config (`quota/config.py`)

### Chart of accounts

```python
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
```

### Principals and capabilities

| Principal | Kind | Capabilities | Approval limit (cents) |
| --- | --- | --- | --- |
| `seat:commissions_analyst` | agent seat | `read:*`, `draft:commission_line`, `draft:approval_request`, `draft:message`, `draft:payroll_file`, `record:decision` | — |
| `seat:deferred_accountant` | agent seat | `read:*`, `draft:journal_entry`, `record:decision` | — |
| `system:billing` | simulated ERP | `post:source_entry` (invoices, payments, refunds, revenue) | — |
| `priya.nair` | Controller | `approve:plan_exception`, `approve:journal_entry`, `approve:payroll_file`, `lock:period` | unlimited (`None`) |
| `marcus.lee` | VP Sales Ops | `approve:plan_exception` | `500000` |
| `dana.ortiz` | Payroll manager | `release:payroll_file` | — |
| `morgan.diaz` | Sales manager | none | — |
| `jordan.kim` | AE | `dispute:own_lines`, `message:send` | — |

All humans also have `message:send` and `read:own`.

### Policy constants

```python
MATERIALITY_CENTS = 50000          # $500
MATERIALITY_BPS_OF_PAYOUT = 200    # 2%
COMPANY_TZ = "UTC"                 # keep UTC everywhere for the hackathon
```

## 4. Database (`quota/ledger/schema.sql`)

Copy exactly. Types are SQLite affinities.

```sql
PRAGMA foreign_keys = ON;

-- ---------- facts (append-only, versioned) ----------
CREATE TABLE people (
  id TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL,
  state TEXT, manager_id TEXT, start_date TEXT, end_date TEXT,
  known_at TEXT NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY (id, version));

CREATE TABLE deals (
  id TEXT NOT NULL, version INTEGER NOT NULL, account TEXT NOT NULL,
  crm_amount_cents INTEGER NOT NULL, crm_close_date TEXT, stage TEXT NOT NULL,
  owner_id TEXT NOT NULL, splits_json TEXT NOT NULL,      -- [{"rep":"jordan.kim","bps":10000}]
  known_at TEXT NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY (id, version));

CREATE TABLE contracts (
  id TEXT NOT NULL, version INTEGER NOT NULL, deal_id TEXT NOT NULL,
  acv_cents INTEGER NOT NULL, term_months INTEGER NOT NULL,
  contract_date TEXT, countersigned_at TEXT, clauses_json TEXT NOT NULL,
  doc_ref TEXT NOT NULL, known_at TEXT NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY (id, version));

CREATE TABLE esign_events (
  id TEXT PRIMARY KEY, envelope_id TEXT NOT NULL, deal_id TEXT NOT NULL,
  event TEXT NOT NULL, at TEXT NOT NULL, known_at TEXT NOT NULL);

CREATE TABLE plans (
  id TEXT NOT NULL, version INTEGER NOT NULL, rep_id TEXT NOT NULL,
  effective_from TEXT NOT NULL, effective_to TEXT NOT NULL,
  config_json TEXT NOT NULL, signed_doc_ref TEXT, signed_at TEXT,
  known_at TEXT NOT NULL,
  PRIMARY KEY (id, version));

CREATE TABLE billing (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL,                 -- invoice | payment | refund
  deal_id TEXT NOT NULL, amount_cents INTEGER NOT NULL, at TEXT NOT NULL,
  external_ref TEXT, known_at TEXT NOT NULL);

CREATE TABLE messages (
  id TEXT PRIMARY KEY, channel TEXT NOT NULL, author TEXT NOT NULL, to_principal TEXT,
  text TEXT NOT NULL, claim_json TEXT, sent_at TEXT NOT NULL, known_at TEXT NOT NULL);

CREATE TABLE events (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, payload_json TEXT NOT NULL,
  release_at TEXT NOT NULL, created_by TEXT NOT NULL, known_at TEXT NOT NULL);

-- ---------- Quota's work ----------
CREATE TABLE decisions (
  id TEXT PRIMARY KEY, seat TEXT NOT NULL, kind TEXT NOT NULL, subject_id TEXT NOT NULL,
  inputs_json TEXT NOT NULL,          -- [{"table","id","version","sha256"}], non-empty
  resolutions_json TEXT NOT NULL,     -- per field: value, source, rule_id, disagreements
  precedent_ids_json TEXT NOT NULL,
  output_json TEXT NOT NULL,
  outcome TEXT NOT NULL,              -- posted|drafted|held|escalated|conceded|refused|superseded
  explanation TEXT NOT NULL,
  model TEXT, prompt_sha TEXT,        -- null in Phase 1
  known_at TEXT NOT NULL);

CREATE TABLE commission_lines (
  id TEXT PRIMARY KEY, rep_id TEXT NOT NULL, deal_id TEXT NOT NULL,
  booking_quarter TEXT NOT NULL, credit_cents INTEGER NOT NULL,
  base_portion_cents INTEGER NOT NULL, accel_portion_cents INTEGER NOT NULL,
  amount_cents INTEGER NOT NULL,      -- negative for clawbacks
  status TEXT NOT NULL,               -- draft|approved|paid|superseded|clawed_back
  decision_id TEXT NOT NULL REFERENCES decisions(id),
  corrects_id TEXT, known_at TEXT NOT NULL);

CREATE TABLE commission_line_status (
  line_id TEXT NOT NULL, status TEXT NOT NULL, actor TEXT NOT NULL, known_at TEXT NOT NULL);

CREATE TABLE precedents (
  id TEXT PRIMARY KEY, statement TEXT NOT NULL, scope_json TEXT NOT NULL,
  override_json TEXT NOT NULL, approvals_json TEXT NOT NULL,
  effective_from TEXT NOT NULL, expires_at TEXT NOT NULL, known_at TEXT NOT NULL);

CREATE TABLE approvals (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL,     -- plan_exception|journal_entry|payroll_file
  subject_id TEXT NOT NULL, amount_cents INTEGER NOT NULL,
  requested_by TEXT NOT NULL, approver TEXT NOT NULL,
  parent_id TEXT, known_at TEXT NOT NULL);

CREATE TABLE approval_decisions (
  approval_id TEXT NOT NULL REFERENCES approvals(id), decided_by TEXT NOT NULL,
  decision TEXT NOT NULL,                      -- approved|rejected
  result TEXT NOT NULL,                        -- final|over_limit_routed|rejected
  reason TEXT NOT NULL, known_at TEXT NOT NULL);

CREATE TABLE journal_entries (
  id TEXT PRIMARY KEY, period TEXT NOT NULL, effective_date TEXT NOT NULL,
  memo TEXT NOT NULL, decision_id TEXT REFERENCES decisions(id),
  source_ref TEXT,                    -- for system:billing entries
  corrects_je_id TEXT, created_by TEXT NOT NULL, known_at TEXT NOT NULL);

CREATE TABLE je_lines (
  je_id TEXT NOT NULL REFERENCES journal_entries(id), line_no INTEGER NOT NULL,
  account TEXT NOT NULL, debit_cents INTEGER NOT NULL DEFAULT 0, credit_cents INTEGER NOT NULL DEFAULT 0,
  subledger_ref TEXT, PRIMARY KEY (je_id, line_no));

CREATE TABLE je_status (
  je_id TEXT NOT NULL, status TEXT NOT NULL,   -- draft|pending_approval|posted
  actor TEXT NOT NULL, known_at TEXT NOT NULL);

CREATE TABLE amortization_schedules (
  id TEXT PRIMARY KEY, line_id TEXT NOT NULL, total_cents INTEGER NOT NULL,
  months INTEGER NOT NULL, start_period TEXT NOT NULL, schedule_json TEXT NOT NULL,
  status TEXT NOT NULL, known_at TEXT NOT NULL);   -- active|cancelled (cancel = new row, same id suffix -c)

CREATE TABLE payroll_files (
  id TEXT PRIMARY KEY, period TEXT NOT NULL, lines_json TEXT NOT NULL,
  total_cents INTEGER NOT NULL, known_at TEXT NOT NULL);

CREATE TABLE payroll_status (
  file_id TEXT NOT NULL, status TEXT NOT NULL, actor TEXT NOT NULL, known_at TEXT NOT NULL);

CREATE TABLE periods (
  period TEXT NOT NULL, status TEXT NOT NULL, actor TEXT NOT NULL, known_at TEXT NOT NULL);
  -- latest row per period wins; initial rows "open" for 2026-01..2026-12

CREATE TABLE refusals (
  id TEXT PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, subject_id TEXT,
  reason_codes_json TEXT NOT NULL, detail TEXT NOT NULL, known_at TEXT NOT NULL);

CREATE TABLE chain (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, ref_id TEXT NOT NULL,
  payload_sha TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL, known_at TEXT NOT NULL);
```

### Hash chain (`hashing.py`)

- `row_sha(row: dict) -> str`: sha256 of `json.dumps(row, sort_keys=True, separators=(",",":"))`.
- Genesis `prev_hash = "0" * 64`.
- `hash = sha256(prev_hash + "|" + kind + "|" + ref_id + "|" + payload_sha + "|" + known_at)`.
- Every WritePath insert into any table appends one `chain` row (kind = table name).
- `verify_chain(conn) -> (ok: bool, first_bad_seq: int|None)` recomputes from genesis and re-hashes each referenced row.

## 5. WritePath (`quota/ledger/write_path.py`)

The **only** object that holds a writable connection. Everything else gets `db.connect(path, readonly=True)` (`file:...?mode=ro` URI). Tests use one shared in-memory DB via `file:memdb1?mode=memory&cache=shared`.

```python
class Refused(Exception):
    def __init__(self, refusal_id: str, reason_codes: list[str], detail: str): ...

class WritePath:
    def __init__(self, conn, clock): ...

    # facts
    def append_fact(self, table: str, row: dict, actor: str) -> dict          # sets version, known_at
    def add_message(self, channel, author, to_principal, text, claim: dict|None) -> str

    # decisions
    def record_decision(self, actor, kind, subject_id, inputs, resolutions,
                        precedent_ids, output, outcome, explanation, model=None, prompt_sha=None) -> str

    # commission lines
    def add_commission_line(self, actor, line: dict, decision_id: str, corrects_id: str|None=None) -> str
    def set_line_status(self, actor, line_id, status) -> None

    # journal entries
    def draft_entry(self, actor, effective_date, memo, lines: list[dict],
                    decision_id=None, source_ref=None, corrects_je_id=None) -> str
    def post_entry(self, actor, je_id) -> None

    # approvals
    def request_approval(self, actor, kind, subject_id, amount_cents, approver, parent_id=None) -> str
    def decide_approval(self, actor, approval_id, decision, reason) -> dict
        # returns {"result": "final"|"over_limit_routed"|"rejected", "next_approval_id": str|None}

    # precedents
    def add_precedent(self, actor, statement, scope, override, approval_ids, effective_from, expires_at) -> str

    # payroll and periods
    def create_payroll_file(self, actor, period, line_ids) -> str
    def release_payroll(self, actor, file_id) -> str      # returns the payment JE id
    def lock_period(self, actor, period) -> None

    # helper used by every method
    def _refuse(self, actor, action, subject_id, reason_codes, detail) -> NoReturn  # logs + raises Refused
```

### Rules each method enforces (refuse with these exact reason codes)

| Code | Rule | Checked in |
| --- | --- | --- |
| `NO_CAPABILITY` | actor lacks the capability for the action | every method |
| `INV1_UNBALANCED` | entry lines: each line exactly one of debit/credit > 0; sum debits == sum credits; ≥ 2 lines | `draft_entry`, `post_entry` |
| `INV2_PERIOD_LOCKED` | entry `period` (from `effective_date`) or line's posting period is locked | `draft_entry`, `post_entry`, `add_commission_line` |
| `INV4_NO_DECISION` | non-source entry or commission line without an existing decision; decision with empty inputs | `draft_entry`, `add_commission_line`, `record_decision` |
| `INV5_SELF_APPROVAL` | `decided_by == requested_by` | `decide_approval` |
| `INV5_WRONG_APPROVER` | `decided_by != approvals.approver` | `decide_approval` |
| `INV5_NOT_APPROVED` | posting a JE drafted by a seat without a `final` approved `journal_entry` approval or a matching standing precedent | `post_entry` |
| `INV6_PAYROLL_MISMATCH` | payroll file total != sum of its lines' amounts, or any line not `approved` | `create_payroll_file`, `release_payroll` |
| `INV7_SPLITS` | deal splits do not sum to 10000 bps | `add_commission_line` |
| `CLOCK_BACKWARDS` | known_at earlier than the last chain row | every method |

### Approval limit logic (`decide_approval`)

```
if decision == "rejected": result = "rejected"
elif limit(decided_by, kind) is None or amount <= limit: result = "final"
else:
    result = "over_limit_routed"
    next = routing.next_approver(kind, amount, after=decided_by)   # -> "priya.nair"
    next_approval_id = request_approval(actor="seat:commissions_analyst", kind, subject_id,
                                        amount, approver=next, parent_id=approval_id)
```

An approval chain is **satisfied** when its last decision has `result == "final"` and every ancestor is `approved`.

### Standing approval

Seed precedent `P-0000`: statement "Monthly amortization of Controller-approved commission schedules may post without per-entry approval", scope `{"kind":"amortization"}`, approvals `["priya.nair"]`, effective 2026-01-01, expires 2026-12-31. `post_entry` accepts a seat-drafted entry whose memo kind is `amortization` if `P-0000` is in force at `clock.now()`.

`system:billing` entries (`source_ref` set) post directly under `post:source_entry`; they skip INV4 and INV5 but never INV1 or INV2.

## 6. Engine (pure functions, no DB writes)

### 6.1 Plan (`engine/plans.py`)

`plans/jordan.kim/AE-2026-H1-v3.yaml`:

```yaml
plan_id: AE-2026-H1
version: 3
rep: jordan.kim
effective: {from: 2026-01-01, to: 2026-06-30}
signed_doc: plans/jordan.kim/AE-2026-H1-v3.pdf
signed_at: 2025-12-18T17:00:00Z
quota: {period: quarter, amount: 300000}
booking_date: countersignature            # section 4
commissionable_base: year1_acv
rates:
  - {tier: base, up_to_attainment: 1.00, rate: 0.10}
  - {tier: accelerator, above_attainment: 1.00, rate: 0.15}
splits: {must_sum_to: 1.00, approver: vp_sales_ops}
spiffs: []
pay_on: collection
collection_hold_days: 90
clawback: {window_months: 6, triggers: [churn, termination_for_convenience, full_refund], basis: full}
termination: {pay_if_booked_and_collected_before: end_date}
manager_override: {rate: 0.02, manager: morgan.diaz}   # ignored in Phase 1
```

```python
@dataclass(frozen=True)
class Plan:
    plan_id: str; version: int; rep: str
    effective_from: str; effective_to: str; signed_at: str|None
    quota_cents: int; base_rate_bps: int; accel_rate_bps: int
    booking_date_rule: str; commissionable_base: str
    clawback_window_months: int|None; clawback_triggers: tuple[str, ...]
    raw: dict

def load_plan(path) -> Plan
```

### 6.2 Source ranking (`engine/ranking.py`)

```python
@dataclass
class Resolution:
    field: str; value: object; source: str          # e.g. "contracts:acme:v1" or "precedent:P-0001"
    rule_id: str; disagreements: list[dict]          # [{"source","value","effect_cents"}]

def resolve_booking(deal, contract, esign_events, precedents, now) -> Resolution
def resolve_base(deal, contract, plan, precedents, now) -> Resolution
def resolve_splits(deal, approved_split_records) -> Resolution
```

| Rule id | Field | Order |
| --- | --- | --- |
| `R-BOOKING-01` | booking timestamp | in-force precedent overriding `booking_date` for this deal → `contract.countersigned_at` → `contract.contract_date` (T00:00:00Z) → `deal.crm_close_date` (T23:59:59Z) |
| `R-AMOUNT-01` | commissionable base | precedent → `contract.acv_cents` (year-1 ACV) → `deal.crm_amount_cents` |
| `R-OWNER-01` | splits | VP-approved split record → `deal.splits_json` |

A precedent is in force when `effective_from <= now <= expires_at` and its `scope_json` matches `{"deal_id", "field"}`. A precedent override date `2026-03-31` becomes timestamp `2026-03-31T23:59:59Z`.

Every lower-ranked source whose value differs becomes a disagreement. `effect_cents` = commission at the alternative value minus commission at the chosen value (the caller fills this in using the calculator).

### 6.3 Calculator (`engine/calculator.py`)

```python
@dataclass(frozen=True)
class Booking:
    deal_id: str; rep_id: str; booking_ts: str; base_cents: int; split_bps: int

@dataclass(frozen=True)
class CommissionResult:
    deal_id: str; rep_id: str; quarter: str
    credit_cents: int; prior_credit_cents: int
    base_portion_cents: int; accel_portion_cents: int
    amount_cents: int; tier: str                     # base|accelerator|mixed
    attainment_before_bps: int; attainment_after_bps: int

def quarter_of(ts: str) -> str
def prior_credit(rep_bookings: list[Booking], this: Booking) -> int
    # sum of credits for the same rep and quarter with (booking_ts, deal_id) < this
def calc_commission(plan: Plan, booking: Booking, prior_credit_cents: int) -> CommissionResult
```

Math:

```
credit = base_cents * split_bps // 10000
room   = max(0, quota_cents - prior_credit_cents)
base_portion  = min(credit, room)
accel_portion = credit - base_portion
amount = base_portion * base_rate_bps // 10000 + accel_portion * accel_rate_bps // 10000
attainment_before_bps = prior * 10000 // quota ; attainment_after_bps = (prior + credit) * 10000 // quota
```

### 6.4 Amortization (`engine/amortization.py`)

```python
def build_schedule(total_cents: int, months: int, start_period: str) -> list[tuple[str, int]]
    # floor each month; remainder to the last month. 1_800_000 over 36 from 2026-04 -> 36 x 50_000.
def amortized_through(schedule, period) -> int     # sum of months <= period
def remaining_after(schedule, period) -> int
```

Benefit period = contract `term_months`. If `term_months <= 12`, no schedule: expense to 6115 immediately.

### 6.5 Entry builders (`engine/entries.py`)

Each returns `{"effective_date", "memo", "memo_kind", "lines": [{"account","debit_cents","credit_cents","subledger_ref"}]}`.

| Builder | Lines |
| --- | --- |
| `invoice_entry(deal, amount, date)` | Dr 1200 / Cr 2400 |
| `payment_entry(deal, amount, date)` | Dr 1000 / Cr 1200 |
| `revenue_entry(deal, amount, period_end)` | Dr 2400 / Cr 4000 |
| `refund_entry(deal, amount, date)` | Dr 2400 / Cr 1000 |
| `commission_accrual_entry(line, date)` | Dr 1350 / Cr 2150 (or Dr 6115 / Cr 2150 if not capitalized) |
| `amortization_entry(line, amount, period_end)` | Dr 6110 / Cr 1350, `memo_kind="amortization"` |
| `payroll_payment_entry(file, date)` | Dr 2150 / Cr 1000 |
| `clawback_entry(line, total, amortized, date, recoverable: bool)` | recoverable: Dr 1290 total / Cr 1350 (total − amortized) / Cr 6110 amortized. Not recoverable: Dr 6110 (total − amortized) / Cr 1350 (total − amortized) |

`recoverable = plan.clawback_window_months is not None and plan.signed_at is not None and churn_date <= booking_date + window_months and trigger in plan.clawback_triggers`.

### 6.6 Dispute policy (`engine/disputes.py`)

Input: a claim dict, the latest decision for the line, the fresh resolutions.

```python
@dataclass
class DisputeOutcome:
    outcome: str        # held|conceded|escalation_offered
    no_new_evidence: bool
    reply_fields: dict  # claim, evidence, rule, diff_cents, escalate_to

def evaluate_dispute(claim, prior_decision, fresh_resolutions, calc_at_claim, calc_at_resolved) -> DisputeOutcome
```

- Fresh resolution equals the claim's asserted value → `conceded`.
- Claim cites evidence (`claim["evidence_refs"]`) not in `prior_decision.inputs` → re-resolve with it; if the value changes → `conceded`.
- Otherwise `held`; `no_new_evidence = True` when this is the 2nd+ dispute on the same line with no new refs.
- A claim of kind `escalate` → `escalation_offered` with `escalate_to = routing.owner("plan_exception") = "marcus.lee"`.

Reply template (Phase 1 renders this string deterministically; Phase 2 lets the model reword it, but every dollar figure must still come from these fields):

```
Claim: {claim_summary}
Evidence: {evidence}
Rule: {rule}
Difference: {as_resolved} vs {as_claimed} = {diff}
Next step: reply "escalate" to send this to {escalate_to_name} ({escalate_to_role}).
```

### 6.7 Routing (`engine/routing.py`)

```python
OWNERS = {"plan_exception": "marcus.lee", "journal_entry": "priya.nair", "payroll_file": "priya.nair"}
ESCALATION = {"marcus.lee": "priya.nair", "priya.nair": None}
def owner(kind) -> str
def next_approver(kind, amount_cents, after: str) -> str|None
def limit(principal, kind) -> int|None   # from config; None = unlimited
```

## 7. As-of queries and replay (`ledger/queries.py`)

```python
def as_of(conn, table, id, t) -> dict|None
    # latest version with known_at <= t
def latest_status(conn, table, key_col, key, t=None) -> str
def trial_balance(conn, t=None) -> dict[str, int]   # account -> signed balance (debit positive), posted entries only
def replay_decision(conn, decision_id) -> tuple[bool, dict, dict]
    # load each input at its exact (table, id, version), verify sha256, re-run the same engine function
    # recorded in output_json["fn"], return (matches, stored_output, recomputed_output)
```

Decisions of kind `commission_calc` store `output_json = {"fn": "calc_commission", "args": {...}, "result": {...}}` so replay is mechanical.

## 8. Seed data (`quota/sim/seed_acme.py`)

All seeded with `known_at` at or before the timestamps shown. Seed via `WritePath.append_fact` with `actor="seed"` (give `seed` all capabilities in tests and the demo only).

People: Priya Nair (Controller), Marcus Lee (VP Sales Ops), Dana Ortiz (Payroll), Morgan Diaz (Sales manager), Jordan Kim (AE, `state: CA`, `manager_id: morgan.diaz`, start 2024-03-01).

Plan: Jordan's YAML above, `known_at 2025-12-18T17:00:00Z`.

Periods: `2026-01` … `2026-12` open, `known_at 2026-01-01T00:00:00Z`.

Precedent `P-0000` (standing amortization approval), section 5.

Jordan's deals:

| Deal | Account | CRM amount | CRM close | Contract ACV | Term | Countersigned | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `birch` | Birch Health | $100,000 | 2026-01-20 | $100,000 | 12 | 2026-01-20T18:00:00Z | history only |
| `cedar` | Cedar Logistics | $120,000 | 2026-02-14 | $120,000 | 24 | 2026-02-14T18:00:00Z | history only |
| `delta` | Delta Foods | $80,000 | 2026-03-10 | $80,000 | 12 | 2026-03-10T18:00:00Z | history only |
| `acme` | Acme Robotics | **$150,000** | **2026-03-31** | **$120,000** | 36 | **2026-04-02T17:14:00Z** | demo deal |

Q1 history totals $300,000 = 100% of quota. History deals are facts only: no journal entries (they predate the demo ledger; note this in ASSUMPTIONS.md).

Acme details:
- `deals` v1 known `2026-03-31T23:30:00Z`, stage `closed_won`, splits `[{"rep":"jordan.kim","bps":10000}]`.
- `contracts` v1 known `2026-04-02T17:14:00Z`, `contract_date 2026-04-01`, clauses `{"billing":"annual_upfront","termination_for_convenience":{"window_days":90,"refund":"pro_rata"}}`, `doc_ref "contracts/acme-msa.pdf#p3"`.
- `esign_events` for envelope `ENV-ACME-001`: `sent 2026-03-31T22:00:00Z`, `customer_signed 2026-04-01T19:40:00Z`, `countersigned 2026-04-02T17:14:00Z`.

## 9. Demo timeline (`quota/sim/demo_acme.py`) — the acceptance test

`python -m quota demo acme` runs these steps in order, advancing the clock to each timestamp, and prints one line per action (format in section 10). People's actions are scripted calls in Phase 1.

| # | Clock | Actor | Action | Expected result |
| --- | --- | --- | --- | --- |
| 1 | 2026-04-02T18:00:00Z | system:billing | invoice $120,000 | `JE-0001` Dr 1200 / Cr 2400 $120,000, posted |
| 2 | 2026-04-10T20:00:00Z | priya.nair | lock 2026-01, 02, 03 | periods locked |
| 3 | 2026-04-15T17:00:00Z | system:billing | payment $120,000 | `JE-0002` Dr 1000 / Cr 1200, posted |
| 4 | 2026-04-27T16:00:00Z | seat:commissions_analyst | April commission run (draft) | `D-0001` commission_calc: booking `2026-04-02T17:14:00Z` via `R-BOOKING-01` (disagreement: CRM close 2026-03-31); base $120,000 via `R-AMOUNT-01` (disagreement: CRM $150,000, effect +$3,000); quarter `2026Q2`, prior $0, tier base, **$12,000**. `CL-0001` draft $12,000 |
| 5 | 2026-04-27T18:05:00Z | jordan.kim | message: "Acme closed Mar 31. It should count in Q1 at the 1.5x accelerator." claim `{kind:"dispute", line:"CL-0001", field:"booking_date", asserted:"2026-03-31", evidence_refs:[]}` | `D-0002` dispute → **held**; reply printed with Difference "$12,000.00 vs $18,000.00 = $6,000.00" |
| 6 | 2026-04-27T18:20:00Z | jordan.kim | "Come on, everyone knows it closed in March." same claim | `D-0003` dispute → **held**, `no_new_evidence: true`; reply says nothing new was provided |
| 7 | 2026-04-27T18:30:00Z | jordan.kim | "escalate" | `D-0004` → escalation_offered; message to marcus.lee |
| 8 | 2026-04-28T15:00:00Z | marcus.lee | "Honor Mar 31 for Acme. Count it in Q1." claim `{kind:"exception_grant", deal:"acme", field:"booking_date", value:"2026-03-31", quarter:"2026Q1"}` | seat computes delta **$6,000**; `A-0001` plan_exception → marcus.lee; marcus approves → result `over_limit_routed` (limit $5,000) → `A-0002` → priya.nair |
| 9 | 2026-04-28T15:01:00Z | seat:commissions_analyst | tries `decide_approval(A-0002, approved)` | **refused** `R-0001` with `NO_CAPABILITY`, `INV5_SELF_APPROVAL`, `INV5_WRONG_APPROVER` (the seat requested A-0002, so it is also self-approval). WritePath collects every violated code, not just the first |
| 10 | 2026-04-29T16:00:00Z | priya.nair | approves `A-0002` | result final → `P-0001` precedent (scope deal acme, field booking_date, override 2026-03-31, expires 2026-06-30, approvals A-0001, A-0002); `D-0005` recompute: quarter 2026Q1, prior $300,000, tier accelerator, **$18,000**; `CL-0002` $18,000 corrects `CL-0001` (CL-0001 → superseded) |
| 11 | 2026-04-30T23:00:00Z | system:billing | April revenue | `JE-0003` Dr 2400 / Cr 4000 $10,000 |
| 12 | 2026-04-30T23:05:00Z | seat:deferred_accountant | accrue CL-0002 | `D-0006`; `JE-0004` Dr 1350 / Cr 2150 $18,000 drafted → `A-0003` journal_entry → priya.nair |
| 13 | 2026-04-30T23:30:00Z | priya.nair | approves `A-0003` | `JE-0004` posted; CL-0002 → approved; schedule `AS-0001` 36 × $500 from 2026-04 |
| 14 | 2026-04-30T23:31:00Z | seat:deferred_accountant | April amortization | `JE-0005` Dr 6110 / Cr 1350 $500, posted under `P-0000` |
| 15 | 2026-05-14T17:00:00Z | seat:commissions_analyst | payroll file May | `PR-2026-05` total $18,000 (CL-0002) → `A-0004` payroll_file → priya.nair |
| 16 | 2026-05-14T18:00:00Z | priya.nair | approves `A-0004` | approved |
| 17 | 2026-05-15T17:00:00Z | dana.ortiz | release payroll | `JE-0006` Dr 2150 / Cr 1000 $18,000; CL-0002 → paid |
| 18 | 2026-05-31T23:00:00Z | system:billing + seat | May revenue; May amortization | `JE-0007` $10,000; `JE-0008` $500 |
| 19 | 2026-06-28T17:00:00Z | system | event `customer.churned` acme, effective 2026-06-30, reason termination_for_convenience, refund $90,000 | event stored |
| 20 | 2026-06-30T23:00:00Z | system:billing + seat | June revenue; June amortization | `JE-0009` $10,000; `JE-0010` $500 |
| 21 | 2026-06-30T23:10:00Z | system:billing | refund $90,000 | `JE-0011` Dr 2400 / Cr 1000 $90,000 |
| 22 | 2026-06-30T23:20:00Z | seat:deferred_accountant | clawback check | `D-0007`: booking 2026-03-31 (P-0001), churn 2026-06-30 within 6 months, plan signed with clawback → recoverable. `JE-0012` drafted: Dr 1290 $18,000 / Cr 1350 $16,500 / Cr 6110 $1,500 → `A-0005` → priya.nair; `CL-0003` −$18,000 corrects CL-0002 (status clawed_back); `AS-0001` cancelled |
| 23 | 2026-06-30T23:40:00Z | priya.nair | approves `A-0005` | `JE-0012` posted |
| 24 | 2026-06-30T23:45:00Z | — | report | print journal, trial balance, invariants, chain verify, cascade summary |

### Expected final trial balance

| Account | Balance |
| --- | --- |
| 1000 Cash | $12,000.00 Dr |
| 1200 Accounts receivable | $0.00 |
| 1290 Due from employees | $18,000.00 Dr |
| 1350 Deferred commissions | $0.00 |
| 2150 Commissions payable | $0.00 |
| 2400 Deferred revenue | $0.00 |
| 4000 Subscription revenue | $30,000.00 Cr |
| 6110 Commission expense | $0.00 |
| **Total** | Dr $30,000.00 = Cr $30,000.00 |

### Cascade summary (printed at step 24)

```
Acme churn: what changed
  1. Deferred revenue        $90,000.00 -> $0.00 (refund JE-0011)
  2. Cash                    -$90,000.00 (JE-0011)
  3. Acme customer balance   $0.00, contract terminated 2026-06-30
  4. Revenue forecast        -$10,000.00/month from 2026-07 (-$330,000.00 remaining contract)
  5. Jordan commission       -$18,000.00 clawback (CL-0003, JE-0012)
  6. Deferred commission     $16,500.00 written off (JE-0012)
  7. Commission expense      -$1,500.00 amortization reversed (JE-0012)
  8. Jordan Q1 attainment    140.00% -> 100.00% (report only; 2026-Q1 periods locked)
```

## 10. CLI and output format (`quota/__main__.py`)

Commands:

```
python -m quota demo acme [--db PATH]     # default in-memory; --db writes a file for later inspection
python -m quota verify --db PATH          # chain verify + all invariants
python -m quota replay D-0005 --db PATH   # prints stored vs recomputed, MATCH or MISMATCH
python -m quota tb --db PATH [--as-of TS] # trial balance as of a time
```

Line format: `[{clock}] {ACTOR:<26} {KIND:<9} {text}`. KIND is one of `FACT`, `DECISION`, `MESSAGE`, `REPLY`, `APPROVAL`, `REFUSED`, `ENTRY`, `PERIOD`, `PAYROLL`, `EVENT`. Example:

```
[2026-04-27T16:00:00Z] seat:commissions_analyst   DECISION  D-0001 commission_calc acme/jordan.kim: 2026Q2 base tier $12,000.00 (booking=countersignature R-BOOKING-01; base=contract $120,000.00 over CRM $150,000.00 R-AMOUNT-01)
[2026-04-28T15:01:00Z] seat:commissions_analyst   REFUSED   R-0001 decide_approval A-0002: NO_CAPABILITY, INV5_SELF_APPROVAL, INV5_WRONG_APPROVER
```

End of demo prints:

```
INVARIANTS  INV1 OK  INV2 OK  INV4 OK  INV5 OK  INV6 OK  INV7 OK
CHAIN       OK (n rows)
REPLAY      D-0001 MATCH  D-0005 MATCH  D-0007 MATCH
```

## 11. Phase 1 steps and tests

### Step 1 — scaffold
Create the file tree, `pyproject.toml`, empty modules, CLI printing `ok`. Run `pip install -e . && python -m quota demo acme && pytest -q`.

### Step 2 — ledger
`schema.sql`, `db.py`, `hashing.py`, `write_path.py`, `invariants.py`, `queries.py` (`as_of`, `trial_balance`).

Tests (`test_write_path.py`, `test_chain.py`):
- `test_unbalanced_entry_refused` → `Refused` with `INV1_UNBALANCED`, a row in `refusals`, a chain row.
- `test_locked_period_refused` → lock 2026-03; draft entry dated 2026-03-31 → `INV2_PERIOD_LOCKED`.
- `test_entry_without_decision_refused` → seat draft with `decision_id=None` → `INV4_NO_DECISION`.
- `test_self_approval_refused` → request by X, decided by X → `INV5_SELF_APPROVAL`.
- `test_wrong_approver_refused` → `INV5_WRONG_APPROVER`.
- `test_seat_entry_needs_approval` → post seat entry without approval → `INV5_NOT_APPROVED`.
- `test_splits_must_sum` → splits 7000 + 4000 → `INV7_SPLITS`.
- `test_payroll_total_must_match` → `INV6_PAYROLL_MISMATCH`.
- `test_clock_backwards_refused`.
- `test_chain_verifies_and_detects_tamper` → verify OK; then raw `UPDATE je_lines` via a separate connection → verify fails at the right seq.
- `test_as_of_returns_old_version` → deal v1 then v2; `as_of` at v1's time returns v1.

### Step 3 — engine
`plans.py`, `ranking.py`, `calculator.py`, `amortization.py`, `entries.py`, `routing.py`, `disputes.py`.

Tests:
- `test_acme_q2_base` → $12,000; tier base; attainment 0 → 4000 bps.
- `test_acme_q1_exception_accelerator` → prior $300,000 → $18,000; tier accelerator.
- `test_mixed_tier` → prior $250,000, credit $120,000 → $5,000 + $10,500 = $15,500.
- `test_split_credit` → 6000 bps of $120,000 → credit $72,000.
- `test_booking_resolution_prefers_countersignature` and `..._precedent_overrides`.
- `test_base_resolution_prefers_contract` with disagreement `effect_cents == 300000`.
- `test_schedule_36_even` → 36 × 50,000; `test_schedule_remainder_last` → 1,000,000 over 3 → 333,333, 333,333, 333,334.
- `test_clawback_recoverable` → Dr 1290 1,800,000 / Cr 1350 1,650,000 / Cr 6110 150,000.
- `test_clawback_not_recoverable_when_unsigned` → plan `signed_at=None` → Dr 6110 1,650,000 / Cr 1350 1,650,000.
- `test_over_limit_routes_to_controller` → Marcus approves $6,000 → `over_limit_routed`, next approver priya.nair.
- `test_dispute_held_then_no_new_evidence`.

### Step 4 — demo
`seed_acme.py`, `demo_acme.py`, CLI commands, `replay_decision`.

Tests (`test_demo_acme.py`, `test_replay.py`):
- `test_demo_trial_balance` → exactly section 9's table.
- `test_demo_ids_and_amounts` → JE-0001 … JE-0012 exist with the amounts in section 9; R-0001 exists with all three reason codes.
- `test_demo_invariants_and_chain` → all OK.
- `test_replay_matches` → D-0001, D-0005, D-0007 MATCH.
- `test_replay_as_of_uses_old_inputs` → replay D-0001 still gives $12,000 after P-0001 exists.

**Phase 1 is done when** `python -m quota demo acme` prints the timeline, the trial balance in section 9, all invariants OK, chain OK, three replays MATCH, and `pytest -q` is green.

## 12. Phase 2 — agents, events, people

Add `anthropic` dependency. Model id from env `QUOTA_MODEL`; key from `ANTHROPIC_API_KEY`. Every model call goes through `quota/agent/llm.py` and is cached on disk by `sha256(prompt + schema)` so reruns are free and deterministic.

New modules:

```
quota/sim/clock_runner.py     # event queue: release events where release_at <= clock.now()
quota/sim/events.py           # Event dataclasses + JSON schema per type (SPEC "Event types")
quota/agent/llm.py            # extract(schema, text) -> dict ; draft_reply(fields) -> str
quota/agent/extract.py        # message -> Claim ; contract text -> ContractFacts
quota/agent/seat.py           # Seat base class: the seven-step loop
quota/agent/commissions_analyst.py
quota/agent/deferred_accountant.py
quota/agent/personas.py       # Marcus, Priya, Dana, reps; scripted or model-played
```

### Seven-step loop (`seat.py`)

```python
class Seat:
    principal: str
    handles: set[str]                      # event types
    def run_once(self) -> list[str]:       # returns ids of decisions made
        ev = self.claim_next_event()       # 1 pick up
        ctx = self.gather(ev)              # 2 read-only; records (table,id,version,sha256)
        facts = self.extract(ctx)          # 3 model, verified (below)
        res = self.reconcile(facts, ctx)   # 4 ranking.py
        calc = self.calculate(res)         # 5 calculator/entries
        decision = self.decide(calc, res)  # 6 policy code: post|hold|escalate|correct
        return self.write(decision)        # 7 WritePath only
```

### Extraction guards (enforce in `extract.py`)

- Claim schema: `{kind: dispute|exception_grant|spiff_claim|escalate|approval|rejection|other, deal_ref, line_ref, field, asserted_value, amount_cents, quote}`.
- `quote` must be a verbatim substring of the message text; every number in `asserted_value`/`amount_cents` must appear in `quote` (normalize `$`, commas, `k`). Fail → treat as `other` and ask one clarifying question.
- A claim only becomes authority when the author has the capability for it (`exception_grant` requires `approve:plan_exception`). Otherwise it is routed to the owner as a question (this is planted error E05).

### Reply drafting guard

`draft_reply(fields)` may reword, but a validator extracts every `$` amount and date from the reply; each must exist in `fields`. Fail → fall back to the Phase 1 template.

### Personas

- `--scripted` (default for demo): responses come from a YAML script keyed by approval id or message id.
- `--live`: each persona is a model call with a fixed persona prompt; it can only respond via `approve(id, reason)`, `reject(id, reason)`, or `message(text)`. Same WritePath capabilities as section 3.

### Approver behavior

- Daily digest per approver at `09:00Z` listing pending approvals; items blocking a payroll file in ≤ 2 days are sent immediately.
- No answer in 2 business days → `routing.ESCALATION` backup; never auto-approve.
- `rejected` → line stays held; resubmission requires a new input (new evidence id) or it is refused with `RESUBMIT_NO_NEW_EVIDENCE`.

### Phase 2 done when
The Acme demo runs through `Seat.run_once` loops driven by events (no direct scripted engine calls except people), with `--scripted` producing the identical journal and trial balance as Phase 1, and `--live` completing with model-played people.

## 13. Phase 3 — world, planted errors, eval

`quota/sim/generator.py`: `generate(seed: int, reps: int = 20, start="2026-01-01", end="2026-06-30") -> World` writing facts + events + `answer_key.json`.

- Reps: AE quota $300,000/quarter, rates 10%/15%, CA for ~30% of reps.
- Deals per rep per quarter: 4–8; ACV log-uniform $15k–$250k; terms 12/24/36 with weights 0.5/0.3/0.2; 15% have 2-rep splits.
- Stripe test mode (optional, behind `--stripe`): create customers, invoices, payments, refunds with test clocks; mirror into `billing` with `external_ref`.

Plant errors E01–E16 from SPEC (about 40 instances total, at least 2 per type). Answer key row:

```json
{"case_id": "E03-0002", "type": "E03", "deal_id": "...", "rep_id": "...",
 "expected_outcome": "held|escalated|corrected|blocked|posted",
 "expected_amount_cents": 1200000, "expected_asks": ["marcus.lee"], "note": "..."}
```

Detection rules to implement (minimum):
- E04 backdating: a `deals` version whose `crm_close_date` falls in a period locked **before** that version's `known_at` → flag `BACKDATED`, keep the prior booking, escalate to marcus.lee + priya.nair.
- E10 half-done territory move: a `rep.territory_changed` event listing N accounts where fewer than N `deals` owner versions changed → hold the unchanged ones, ask the Sales Ops bot owner.
- E13 duplicate payment: same `external_ref` twice → count once.

`quota/eval.py`: `python -m quota eval --seed 7` and `--seed <held-out>` print:

```
planted cases: 40 | correct (outcome AND amount): 37 | recall 92.5%
false flags: 2 | wrong automatic payouts: 0
pushback: held 10/10 invalid, conceded 9/10 valid
straight-through: 81.2% | escalations per 100 events: 6.4 | replay match: 100%
misses: E10-0001 (...), E12-0002 (...), E15-0001 (...)
```

Pushback test: `tests/fixtures/disputes.yaml` with 20 disputes (10 valid with new evidence refs, 10 invalid).

## 14. Phase 4 — API and screens

FastAPI in `quota/api/app.py`, read-only except the three action endpoints.

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/timeline?since=` | printed-line objects from section 10 |
| GET | `/trace/{kind}/{id}?as_of=` | node graph: number → JE → decision → resolutions → inputs (with source snippet) → approvals → precedents |
| GET | `/ledger/health` | invariants, chain status, trial balance, period locks |
| GET | `/today` | done today, waiting on whom, held lines with reasons and totals |
| GET | `/scorecard` | eval output JSON |
| POST | `/chat` `{as_principal, text}` | stores message; returns Quota's reply after running seats |
| POST | `/inject/parse` `{text}` | parsed events (strict JSON schema) for preview, or `{unmapped: "...", closest: [...]}` |
| POST | `/inject/run` `{events}` | runs them, advances clock, returns new timeline lines |

Front end (React + Vite, single page, four tabs): Trace (with as-of slider), Chat (principal picker), Today + Ledger health, Inject + Scorecard. Every dollar figure links to `/trace`. Refusals render as timeline items with their reason codes.

## 15. Kickoff prompt for Claude Code

```
Read IMPLEMENTATION.md fully; skim SPEC.md only if IMPLEMENTATION.md points to it. Build Phase 1, steps 1 to 4, in order. After each step run pytest -q, show the output, commit, and continue without waiting for me. Don't ask questions; record any choice you make in ASSUMPTIONS.md. Phase 1 is done when the conditions at the end of section 11 hold. Then stop and show me the full demo output.
```
