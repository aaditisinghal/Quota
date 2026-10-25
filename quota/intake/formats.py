"""How the vendors' exports write values, both ways (pure). The exporter writes with `to_*`, the intake reads
with the transforms in TRANSFORMS; a transform raises ValueError on a value it cannot read."""

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from quota import config

PACIFIC = ZoneInfo(config.COMPANY_TZ)


# ---------- writing ----------

def to_money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def to_decimal(cents: int) -> str:
    return f"{cents / 100:.2f}"


def to_mdy(day: str | None) -> str:
    if not day:
        return ""
    y, m, d = (int(x) for x in day[:10].split("-"))
    return f"{m}/{d}/{y}"


def to_pacific(ts: str) -> str:
    """UTC 2026-01-05T17:30:00Z -> 1/5/2026 9:30 AM (Pacific, no zone written, as Salesforce exports it)."""
    local = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(PACIFIC)
    hour = local.hour % 12 or 12
    return f"{local.month}/{local.day}/{local.year} {hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def to_utc_space(ts: str) -> str:
    return ts.replace("T", " ").rstrip("Z")


def to_stage(stage: str) -> str:
    return stage.replace("_", " ").title()


def to_clauses(clauses: dict) -> str:
    return "; ".join(f"{k}={v if isinstance(v, str) else json.dumps(v, sort_keys=True)}" for k, v in sorted(clauses.items()))


def to_splits(splits: list[dict], names: dict[str, str]) -> str:
    return " / ".join(f"{names.get(s['rep'], s['rep'])} {s['bps'] / 100:g}%" for s in splits)


# ---------- reading ----------

def money(value: str) -> int:
    text = value.strip().replace("$", "").replace(",", "")
    if not text:
        raise ValueError("blank")
    return round(float(text) * 100)


def integer(value: str) -> int:
    return int(value.strip())


def text(value: str) -> str:
    return value.strip()


def date_mdy(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    return datetime.strptime(value, "%m/%d/%Y").strftime("%Y-%m-%d")


def datetime_pacific(value: str) -> str:
    local = datetime.strptime(value.strip(), "%m/%d/%Y %I:%M %p").replace(tzinfo=PACIFIC)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def datetime_utc(value: str) -> str:
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    raise ValueError(f"not a UTC timestamp: {value!r}")


def stage(value: str) -> str:
    if not value.strip():
        raise ValueError("blank")
    return value.strip().lower().replace(" ", "_")


def clauses(value: str) -> dict:
    out = {}
    for part in [p.strip() for p in value.split(";") if p.strip()]:
        key, _, raw = part.partition("=")
        if not key or not _:
            raise ValueError(f"not key=value: {part!r}")
        try:
            out[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            out[key.strip()] = raw.strip()
    return out


def splits(value: str) -> list[dict]:
    """"Jordan Kim 60% / Sam Ruiz 40%" -> [{"name": "Jordan Kim", "bps": 6000}, ...] (names resolved later)."""
    out = []
    for part in [p.strip() for p in value.split("/") if p.strip()]:
        name, _, pct = part.rpartition(" ")
        if not pct.endswith("%") or not name:
            raise ValueError(f"not 'Name NN%': {part!r}")
        out.append({"name": name.strip(), "bps": round(float(pct[:-1]) * 100)})
    if not out:
        raise ValueError("blank")
    return out


def person(value: str) -> str:
    if not value.strip():
        raise ValueError("blank")
    return value.strip()


TRANSFORMS = {"text": text, "money": money, "decimal_money": money, "int": integer, "date_mdy": date_mdy,
              "datetime_pacific": datetime_pacific, "datetime_utc": datetime_utc, "stage": stage,
              "clauses": clauses, "splits": splits, "person": person}
