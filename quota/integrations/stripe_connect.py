"""Split payouts in a Stripe sandbox: Connect for partners and agents, payroll for employees (CUA_TASKS C6).

Two commands, both idempotent and both refusing anything but a sandbox / test key:

- `python -m quota stripe connect-setup` creates one Custom connected account (recipient service agreement,
  transfers only) per partner or agent in the HR roster's "Partners" department, fills
  `requirements.currently_due` with Stripe's test values until it is empty and `capabilities.transfers` is
  active, and saves the ids to `.stripe_accounts.json` (git-ignored). An account is found again by its
  `quota_partner` metadata, so a rerun creates nothing new.
- `python -m quota stripe orbit-split` runs the Orbit Analytics demo deal: the customer pays the ACV
  (a PaymentIntent with `transfer_group="orbit"`), and the Connect portion of the commission goes to Northstar
  as a Transfer with `source_transaction` = the customer's charge. Payroll portions stay in the payroll file;
  here they are listed in the PaymentIntent metadata and in `out/stripe_orbit_split.json`. It then checks that
  `Transfer.list(transfer_group="orbit")` sums to the Connect portion.

Paths are relative to the Quota home: the first directory at or above the repo root that holds `.env`
(so a worktree under `.claude/worktrees/` uses the main checkout's `.env`, roster and output folder).
"""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from quota.sim.stripe_billing import TEST_CARD, StripeSandbox, StripeSandboxError

REPO_ROOT = Path(__file__).resolve().parents[2]
ACCOUNTS_FILE = ".stripe_accounts.json"
ROSTER_FILE = Path("out") / "hr_roster.json"
SPLIT_FILE = Path("out") / "stripe_orbit_split.json"
PARTNERS_DEPARTMENT = "Partners"
MAX_PARTNERS = 4
DASHBOARD = "https://dashboard.stripe.com"

# Stripe's documented test values (docs.stripe.com/connect/testing, /connect/testing-verification).
TEST_DOB = {"day": 10, "month": 11, "year": 1980}
TEST_ADDRESS = {"line1": "123 State St", "city": "Schenectady", "state": "NY", "postal_code": "12345",
                "country": "US"}
TEST_PHONE = "8888675309"
TEST_SSN_LAST_4 = "0000"
TEST_ID_NUMBER = "000000000"
TEST_ID_DOCUMENT = "file_identity_document_success"
TEST_BANK_TOKEN = "btok_us"
TEST_MCC = "7399"                                   # business services, not elsewhere classified
TEST_URL = "https://accessible.stripe.com"          # passes Stripe's URL check in test mode
TOS_IP = "127.0.0.1"


# ---------------------------------------------------------------- the Orbit demo deal

DEAL_ID = "orbit"
DEAL_NAME = "Orbit Analytics"
ACV_CENTS = 200_000_00
COMMISSION_RATE_BPS = 1000                          # 10% of ACV
DECISION = "orbit-split:approved"


@dataclass(frozen=True)
class Recipient:
    key: str
    name: str
    kind: str                                       # "employee" | "agent" | "team_pool"
    route: str                                      # "payroll" | "connect"
    share_bps: int
    pool_members: tuple[str, ...] = ()


ORBIT_SPLIT = (
    Recipient("alex.chen", "Alex Chen", "employee", "payroll", 5000),
    Recipient("riley.shah", "Riley Shah", "employee", "payroll", 2000),
    Recipient("northstar.partners", "Northstar Partners LLC", "agent", "connect", 2000),
    # The roster has no SEs yet; the pool rule splits evenly among two placeholder members.
    Recipient("se.pool", "Solutions Engineering team pool", "team_pool", "payroll", 1000,
              ("Solutions Engineer 1", "Solutions Engineer 2")),
)


def commission_cents(acv_cents: int = ACV_CENTS) -> int:
    return acv_cents * COMMISSION_RATE_BPS // 10000


def split_amounts(total_cents: int, split=ORBIT_SPLIT) -> list[dict]:
    """Each recipient's cents; the shares must sum to 10000 bps and the cents to the total (INV7)."""
    if sum(r.share_bps for r in split) != 10000:
        raise StripeSandboxError("the split does not sum to 10000 bps")
    rows = [{"recipient": r, "amount_cents": total_cents * r.share_bps // 10000} for r in split]
    rows[0]["amount_cents"] += total_cents - sum(row["amount_cents"] for row in rows)   # any rounding cent
    return rows


def dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


# ---------------------------------------------------------------- plumbing

def find_home(start: Path = REPO_ROOT) -> Path:
    if os.environ.get("QUOTA_HOME"):
        return Path(os.environ["QUOTA_HOME"])
    for path in (start, *start.parents):
        if (path / ".env").exists():
            return path
    return start


def _read_key(home: Path) -> str:
    key = os.environ.get("STRIPE_API_KEY")
    env = home / ".env"
    if not key and env.exists():
        for raw in env.read_text(encoding="utf-8").splitlines():
            name, _, value = raw.strip().partition("=")
            if name.strip() == "STRIPE_API_KEY" and value.strip():
                key = value.strip().strip('"').strip("'")
    if not key:
        raise StripeSandboxError(f"needs STRIPE_API_KEY (a sandbox key) in the environment or {env}")
    return key


def connect(home: Path):
    """A Stripe client for a sandbox / test key only; anything else aborts before a request is made."""
    sandbox = StripeSandbox(_read_key(home), "quota-connect")     # raises unless sk_test_ / rk_test_
    v1 = sandbox.client.v1
    if v1.balance.retrieve().livemode:
        raise StripeSandboxError("the key reached a live-mode account; refusing")
    return v1


def _plain(obj):
    """A StripeObject (not a mapping since stripe-python 15) or a dict, as a plain dict."""
    if obj is None:
        return {}
    return obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)


def _get(obj, name, default=None):
    value = _plain(obj).get(name) if obj is not None and not isinstance(obj, str) else None
    return default if value is None else value


def _meta(obj) -> dict:
    return dict(_get(obj, "metadata", {}) or {})


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def dashboard_urls(platform_id: str, path: str) -> dict:
    """Both forms of a test-mode dashboard link: scoped to the sandbox's account id, and the plain one."""
    return {"sandbox": f"{DASHBOARD}/{platform_id}/test/{path}", "test": f"{DASHBOARD}/test/{path}"}


class ConnectNotEnabled(StripeSandboxError):
    """Connect is off, or its platform setup (e.g. the loss-responsibility review) is unfinished."""


NOT_READY = ("Stripe Connect is not ready in this sandbox (enable Connect and finish the platform profile at "
             f"{DASHBOARD}/settings/connect/platform-profile)")


def _connect_error(exc) -> bool:
    text = str(exc).lower()
    return "connect" in text and ("sign up" in text or "signed up" in text or "enable" in text
                                  or "platform" in text)


# ---------------------------------------------------------------- connect-setup

def partners(home: Path) -> list[dict]:
    roster = json.loads((home / ROSTER_FILE).read_text(encoding="utf-8"))
    found = [p for p in roster["people"] if p.get("department") == PARTNERS_DEPARTMENT]
    if not found:
        raise StripeSandboxError(f"{home / ROSTER_FILE} has nobody in department {PARTNERS_DEPARTMENT!r}")
    return found[:MAX_PARTNERS]


def _display_name(person: dict) -> str:
    return f"{person['firstName']} {person['lastName']}".strip()


def _individual(person: dict) -> dict:
    return {"first_name": person["firstName"], "last_name": person["lastName"], "email": person["workEmail"],
            "phone": TEST_PHONE, "dob": TEST_DOB, "ssn_last_4": TEST_SSN_LAST_4,
            "address": {k: v for k, v in TEST_ADDRESS.items() if k != "country"}}


def _business_profile(person: dict) -> dict:
    return {"mcc": TEST_MCC, "url": TEST_URL, "name": _display_name(person),
            "product_description": f"{person['jobTitle']}: commissions on referred deals"}


def _fill_for(requirement: str, person: dict) -> dict | None:
    """The account update that satisfies one `requirements.currently_due` entry, or None if unknown."""
    head, _, rest = requirement.partition(".")
    if head == "individual":
        field, _, sub = rest.partition(".")
        simple = {"first_name": person["firstName"], "last_name": person["lastName"],
                  "email": person["workEmail"], "phone": TEST_PHONE, "ssn_last_4": TEST_SSN_LAST_4,
                  "id_number": TEST_ID_NUMBER, "political_exposure": "none"}
        if field in simple:
            return {"individual": {field: simple[field]}}
        if field == "dob":
            return {"individual": {"dob": TEST_DOB}}
        if field == "address":
            return {"individual": {"address": {k: v for k, v in TEST_ADDRESS.items() if k != "country"}}}
        if field == "verification":
            doc = sub.split(".")[0]                 # document | additional_document
            if doc in ("document", "additional_document"):
                return {"individual": {"verification": {doc: {"front": TEST_ID_DOCUMENT}}}}
        return None
    if head == "business_profile":
        profile = _business_profile(person)
        return {"business_profile": {rest: profile[rest]}} if rest in profile else None
    if requirement == "external_account":
        return {"external_account": TEST_BANK_TOKEN}
    if head == "tos_acceptance":
        return {"tos_acceptance": {"date": int(time.time()), "ip": TOS_IP}}
    return None


def _merge(into: dict, update: dict) -> None:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            _merge(into[key], value)
        else:
            into[key] = value


def _status(account) -> tuple[list[str], str]:
    requirements = _get(account, "requirements", {})
    due = list(_get(requirements, "currently_due", []) or [])
    transfers = _get(_get(account, "capabilities", {}), "transfers", "inactive")
    return due, transfers


def _existing_accounts(v1) -> dict[str, object]:
    by_partner = {}
    for account in v1.accounts.list(params={"limit": 100}).auto_paging_iter():
        partner = _meta(account).get("quota_partner")
        if partner and partner not in by_partner:
            by_partner[partner] = account
    return by_partner


def _create_account(v1, person: dict):
    params = {
        "type": "custom", "country": "US", "business_type": "individual",
        "email": person["workEmail"],
        "capabilities": {"transfers": {"requested": True}},
        "tos_acceptance": {"service_agreement": "recipient", "date": int(time.time()), "ip": TOS_IP},
        "external_account": TEST_BANK_TOKEN,
        "individual": _individual(person),
        "business_profile": _business_profile(person),
        "metadata": {"quota_partner": person["key"], "quota_name": _display_name(person),
                     "quota_department": person["department"], "quota_source": "hr_roster.json"},
    }
    try:
        return v1.accounts.create(params=params)
    except Exception as exc:                        # stripe.StripeError subclasses
        if _connect_error(exc):
            raise ConnectNotEnabled(f"{NOT_READY}: {exc}") from exc
        raise


def _complete(v1, account, person: dict, log, rounds: int = 8, wait_s: float = 60.0):
    """Fill currently_due with test values until it is empty, then wait for transfers to turn active."""
    unknown: list[str] = []
    for _ in range(rounds):
        due, transfers = _status(account)
        if not due:
            break
        update: dict = {}
        unknown = []
        for requirement in due:
            fill = _fill_for(requirement, person)
            if fill is None:
                unknown.append(requirement)
            else:
                _merge(update, fill)
        if not update:
            break
        log(f"    filling {', '.join(due)}")
        account = v1.accounts.update(account.id, params=update)
    deadline = time.time() + wait_s
    while True:
        due, transfers = _status(account)
        if (not due and transfers == "active") or time.time() > deadline:
            break
        time.sleep(2)
        account = v1.accounts.retrieve(account.id)
    if unknown:
        log(f"    no test value known for: {', '.join(unknown)}")
    return account


def connect_setup(home: Path | None = None, log=print) -> dict:
    home = home or find_home()
    v1 = connect(home)
    platform = v1.accounts.retrieve_current()
    try:
        existing = _existing_accounts(v1)
    except Exception as exc:
        if _connect_error(exc):
            raise ConnectNotEnabled(f"{NOT_READY}: {exc}") from exc
        raise
    people = partners(home)
    if len(people) < MAX_PARTNERS:
        log(f"roster has {len(people)} partners/agents (fewer than {MAX_PARTNERS}); using those")
    saved = {"platform_account": platform.id, "livemode": False, "accounts": {}}
    for person in people:
        name = _display_name(person)
        account = existing.get(person["key"])
        if account is None:
            log(f"{name}: creating a Custom connected account")
            account = _create_account(v1, person)
        else:
            log(f"{name}: found {account.id} by metadata quota_partner={person['key']}")
        account = _complete(v1, account, person, log)
        due, transfers = _status(account)
        log(f"  {account.id}  transfers={transfers}  currently_due={due or '[]'}")
        saved["accounts"][person["key"]] = {
            "account_id": account.id, "name": name, "email": person["workEmail"], "job_title": person["jobTitle"],
            "transfers": transfers, "currently_due": due,
            "service_agreement": _get(_get(account, "tos_acceptance", {}), "service_agreement"),
            "dashboard": dashboard_urls(platform.id, f"connect/accounts/{account.id}"),
        }
    saved["dashboard"] = {"connected_accounts": dashboard_urls(platform.id, "connect/accounts/overview")}
    _write_json(home / ACCOUNTS_FILE, saved)
    log(f"saved {len(saved['accounts'])} account ids to {home / ACCOUNTS_FILE}")
    not_ready = [k for k, a in saved["accounts"].items() if a["transfers"] != "active" or a["currently_due"]]
    if not_ready:
        raise StripeSandboxError(f"accounts not ready for transfers: {', '.join(not_ready)}")
    return saved


# ---------------------------------------------------------------- orbit-split

CUSTOMER_EMAIL = "ap@orbit-analytics.example"


def _customer(v1):
    for customer in v1.customers.list(params={"email": CUSTOMER_EMAIL, "limit": 100}).auto_paging_iter():
        if _meta(customer).get("quota_deal") == DEAL_ID:
            return customer, False
    return v1.customers.create(params={
        "name": DEAL_NAME, "email": CUSTOMER_EMAIL, "description": "Quota demo deal (CUA_TASKS C6)",
        "metadata": {"quota_deal": DEAL_ID}}), True


def _split_metadata(rows: list[dict], accounts: dict) -> dict:
    meta = {"quota_deal": DEAL_ID, "deal_name": DEAL_NAME, "acv": dollars(ACV_CENTS),
            "commission_rate_bps": str(COMMISSION_RATE_BPS), "commission_total": dollars(commission_cents()),
            "commission_total_cents": str(commission_cents()), "split_recipients": str(len(rows))}
    for i, row in enumerate(rows, 1):
        r: Recipient = row["recipient"]
        parts = [r.name, f"kind={r.kind}", f"share_bps={r.share_bps}", f"amount={dollars(row['amount_cents'])}",
                 f"route={r.route}"]
        if r.route == "connect":
            parts.append(f"destination={accounts[r.key]['account_id']}")
        if r.pool_members:
            each = row["amount_cents"] // len(r.pool_members)
            parts.append(f"pool=even across {len(r.pool_members)} SEs ({dollars(each)} each)")
        meta[f"split_{i}"] = " | ".join(parts)
    return meta


def _payment(v1, customer, metadata: dict):
    for intent in v1.payment_intents.list(params={"customer": customer.id, "limit": 100}).auto_paging_iter():
        if _meta(intent).get("quota_deal") == DEAL_ID and intent.status == "succeeded":
            if _meta(intent) != {**_meta(intent), **metadata}:
                intent = v1.payment_intents.update(intent.id, params={"metadata": metadata})
            return intent, False
    intent = v1.payment_intents.create(params={
        "amount": ACV_CENTS, "currency": "usd", "customer": customer.id, "payment_method": TEST_CARD,
        "payment_method_types": ["card"], "confirm": True, "transfer_group": DEAL_ID,
        "description": f"{DEAL_NAME}: year-1 ACV", "metadata": metadata})
    if intent.status != "succeeded":
        raise StripeSandboxError(f"the Orbit payment {intent.id} is {intent.status}, not succeeded")
    return intent, True


def _charge_id(intent) -> str:
    charge = _get(intent, "latest_charge")
    if not charge:
        raise StripeSandboxError(f"{intent.id} has no charge")
    return charge if isinstance(charge, str) else charge.id


def _transfers(v1) -> list:
    return list(v1.transfers.list(params={"transfer_group": DEAL_ID, "limit": 100}).auto_paging_iter())


def orbit_split(home: Path | None = None, log=print) -> dict:
    home = home or find_home()
    v1 = connect(home)
    platform = v1.accounts.retrieve_current()
    path = home / ACCOUNTS_FILE
    if not path.exists():
        raise StripeSandboxError(f"{path} is missing; run `python -m quota stripe connect-setup` first")
    accounts = json.loads(path.read_text(encoding="utf-8"))["accounts"]

    total = commission_cents()
    rows = split_amounts(total)
    for row in rows:
        r = row["recipient"]
        if r.route == "connect" and r.key not in accounts:
            raise StripeSandboxError(f"{r.name} has no connected account in {path}")

    customer, new_customer = _customer(v1)
    log(f"customer {customer.id} {DEAL_NAME} ({'created' if new_customer else 'found'})")
    intent, new_intent = _payment(v1, customer, _split_metadata(rows, accounts))
    charge = _charge_id(intent)
    log(f"payment {intent.id} {dollars(intent.amount)} {intent.status}, charge {charge}, transfer_group "
        f"{intent.transfer_group} ({'created' if new_intent else 'found'})")

    existing = {_meta(t).get("quota_line"): t for t in _transfers(v1) if _meta(t).get("quota_line")}
    transfers = []
    for row in rows:
        r: Recipient = row["recipient"]
        if r.route != "connect":
            continue
        line = f"{DEAL_ID}:{r.key}"
        transfer = existing.get(line)
        if transfer is None:
            transfer = v1.transfers.create(params={
                "amount": row["amount_cents"], "currency": "usd", "destination": accounts[r.key]["account_id"],
                "transfer_group": DEAL_ID, "source_transaction": charge,
                "description": f"{DEAL_NAME}: {r.name} {r.share_bps / 100:g}% of the commission",
                "metadata": {"quota_line": line, "decision": DECISION, "payout_file": str(SPLIT_FILE.as_posix()),
                             "deal": DEAL_ID, "share_bps": str(r.share_bps)}},
                options={"idempotency_key": f"quota-{line}-{charge}"})
            log(f"transfer {transfer.id} {dollars(transfer.amount)} -> {transfer.destination} (created)")
        else:
            log(f"transfer {transfer.id} {dollars(transfer.amount)} -> {transfer.destination} "
                f"(found by quota_line={line})")
        transfers.append((row, transfer))

    # ---- verification: Stripe's transfers equal the Connect portions; with payroll, 100% of the commission
    listed = _transfers(v1)
    stripe_sum = sum(t.amount for t in listed)
    connect_sum = sum(row["amount_cents"] for row in rows if row["recipient"].route == "connect")
    payroll_sum = sum(row["amount_cents"] for row in rows if row["recipient"].route == "payroll")
    checks = {
        "stripe_transfers_equal_connect_portion": stripe_sum == connect_sum,
        "payroll_plus_connect_equal_commission": payroll_sum + connect_sum == total,
        "shares_sum_10000_bps": sum(row["recipient"].share_bps for row in rows) == 10000,
    }

    recipients = []
    for row in rows:
        r: Recipient = row["recipient"]
        entry = {"key": r.key, "name": r.name, "kind": r.kind, "route": r.route, "share_bps": r.share_bps,
                 "amount_cents": row["amount_cents"], "amount": dollars(row["amount_cents"])}
        if r.pool_members:
            each = row["amount_cents"] // len(r.pool_members)
            entry["pool_rule"] = "even"
            entry["pool_members"] = [{"name": m, "amount_cents": each} for m in r.pool_members]
        if r.route == "connect":
            transfer = next(t for rw, t in transfers if rw is row)
            entry["account_id"] = accounts[r.key]["account_id"]
            entry["transfer_id"] = transfer.id
            entry["dashboard"] = dashboard_urls(platform.id, f"connect/transfers/{transfer.id}")
        else:
            entry["payroll_line"] = f"{DEAL_ID}:{r.key}"
        recipients.append(entry)

    result = {
        "deal": DEAL_ID, "deal_name": DEAL_NAME, "livemode": False, "platform_account": platform.id,
        "acv_cents": ACV_CENTS, "commission_rate_bps": COMMISSION_RATE_BPS, "commission_cents": total,
        "connect_cents": connect_sum, "payroll_cents": payroll_sum, "stripe_transfer_sum_cents": stripe_sum,
        "ids": {"customer": customer.id, "payment_intent": intent.id, "charge": charge,
                "accounts": {k: a["account_id"] for k, a in accounts.items()},
                "transfers": [t.id for t in listed]},
        "recipients": recipients,
        "checks": checks,
        "dashboard": {
            "connected_accounts": dashboard_urls(platform.id, "connect/accounts/overview"),
            "northstar_account": dashboard_urls(platform.id, f"connect/accounts/{accounts['northstar.partners']['account_id']}"),
            "northstar_transfer": dashboard_urls(platform.id, f"connect/transfers/{transfers[0][1].id}"),
            "orbit_payment": dashboard_urls(platform.id, f"payments/{intent.id}"),
            "customer": dashboard_urls(platform.id, f"customers/{customer.id}"),
            "transfers": dashboard_urls(platform.id, "connect/transfers"),
        },
    }
    _write_json(home / SPLIT_FILE, result)

    log("")
    log(f"{DEAL_NAME}: ACV {dollars(ACV_CENTS)}, commission {dollars(total)} (10%)")
    for entry in recipients:
        dest = entry.get("transfer_id") or entry.get("payroll_line")
        log(f"  {entry['name']:<34} {entry['route']:<8} {entry['share_bps'] / 100:>5g}%  {entry['amount']:>11}  {dest}")
    log(f"Transfer.list(transfer_group={DEAL_ID!r}) = {dollars(stripe_sum)}; Connect portion {dollars(connect_sum)}; "
        f"payroll {dollars(payroll_sum)}; together {dollars(connect_sum + payroll_sum)} of {dollars(total)}")
    log("checks: " + ", ".join(f"{k}={'OK' if v else 'FAIL'}" for k, v in checks.items()))
    log(f"wrote {home / SPLIT_FILE}")
    if not all(checks.values()):
        raise StripeSandboxError("the Orbit split did not verify")
    return result
