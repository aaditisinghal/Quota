"""Plan dataclass + load_plan(path). Plans are config, not prompts."""

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from quota.money import to_bps


@dataclass(frozen=True)
class Plan:
    plan_id: str
    version: int
    rep: str
    effective_from: str
    effective_to: str
    signed_at: str | None
    quota_cents: int
    base_rate_bps: int
    accel_rate_bps: int
    booking_date_rule: str
    commissionable_base: str
    clawback_window_months: int | None
    clawback_triggers: tuple[str, ...]
    raw: dict = field(compare=False, repr=False)


def normalize(value):
    """YAML dates/timestamps -> the project's ISO strings, recursively."""
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return value


def plan_from_config(raw: dict) -> Plan:
    raw = normalize(raw)
    rates = {r["tier"]: to_bps(r["rate"]) for r in raw["rates"]}
    clawback = raw.get("clawback") or {}
    return Plan(
        plan_id=raw["plan_id"], version=raw["version"], rep=raw["rep"],
        effective_from=raw["effective"]["from"], effective_to=raw["effective"]["to"],
        signed_at=raw.get("signed_at"),
        quota_cents=int(raw["quota"]["amount"]) * 100,
        base_rate_bps=rates["base"], accel_rate_bps=rates["accelerator"],
        booking_date_rule=raw["booking_date"], commissionable_base=raw["commissionable_base"],
        clawback_window_months=clawback.get("window_months"),
        clawback_triggers=tuple(clawback.get("triggers") or ()),
        raw=raw,
    )


def load_plan_raw(path) -> dict:
    return normalize(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def load_plan(path) -> Plan:
    return plan_from_config(load_plan_raw(path))


def plan_from_row(row: dict) -> Plan:
    """A `plans` table row -> Plan (config_json holds the normalized YAML; the row says whether it is signed)."""
    return plan_from_config({**json.loads(row["config_json"]), "version": row["version"],
                             "signed_at": row["signed_at"] or None})
