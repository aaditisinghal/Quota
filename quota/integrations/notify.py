"""Email for answers given in the Needs-you inbox: each approve, reject or reply goes out through Resend.

Configuration from the environment (`.env`): RESEND_API_KEY, NOTIFY_EMAIL (the inbox that receives it) and,
optionally, RESEND_FROM (default "Quota <onboarding@resend.dev>", Resend's test sender, which delivers only to
the Resend account's own address until a domain is verified). Without a key nothing is sent and the answer still
stands: email is a notice, never part of the decision.
"""

import html
import os

import httpx

from quota import config
from quota.money import fmt_usd

API = "https://api.resend.com/emails"
DEFAULT_FROM = "Quota <onboarding@resend.dev>"
VERB = {"approve": "approved", "reject": "rejected", "reply": "replied on"}


def configured() -> bool:
    return bool(os.environ.get("RESEND_API_KEY") and os.environ.get("NOTIFY_EMAIL"))


def answer_email(item: dict | None, item_id: str, person: str, decision: str, text: str, outcome: dict) -> dict:
    """Subject and bodies for one answer: who decided what, their note, and what the ledger did with it."""
    who = config.display_name(person)
    kind = (item or {}).get("kind", "item").replace("_", " ")
    deals = ", ".join((item or {}).get("deals") or [])
    amount = fmt_usd(item["impact_cents"]) if item and item.get("impact_cents") else ""
    subject = f"Quota · {who} {VERB.get(decision, decision)} {item_id}" + (f" ({deals}, {amount})" if deals or amount else "")
    result = outcome.get("result")
    if not outcome.get("ok"):
        status = f"Refused: {outcome.get('refused')}"
    elif isinstance(result, dict) and result.get("result") == "final":
        status = "Final: decided, and recorded on the ledger."
    elif isinstance(result, dict) and result.get("result") == "over_limit_routed":
        status = f"Over the approver's limit: routed on as {result.get('next_approval_id')}."
    elif decision == "reply":
        status = "Reply sent to Quota's commissions analyst; the item stays open."
    else:
        status = str(result)
    rows = [("Decision", f"{who} {VERB.get(decision, decision)} {item_id}"), ("Item", kind + (f" · {deals}" if deals else "")),
            ("At stake", amount), ("Note", text or "(none)"), ("Outcome", status),
            ("Question", (item or {}).get("question", ""))]
    lines = [f"{k}: {v}" for k, v in rows if v]
    body_html = "".join(f"<tr><td style='padding:4px 12px 4px 0;color:#57606a;vertical-align:top'>{html.escape(k)}</td>"
                        f"<td style='padding:4px 0'>{html.escape(v)}</td></tr>" for k, v in rows if v)
    return {"subject": subject, "text": "\n".join(lines),
            "html": ("<div style='font-family:Inter,Segoe UI,sans-serif;font-size:14px;color:#1f2328'>"
                     f"<h2 style='font-size:16px;margin:0 0 12px'>{html.escape(subject)}</h2>"
                     f"<table style='border-collapse:collapse'>{body_html}</table>"
                     "<p style='color:#8c959f;font-size:12px;margin-top:16px'>Sent by Quota's commissions desk.</p></div>")}


def send(message: dict) -> dict:
    """{"sent": True, "id", "to"} or {"sent": False, "error"}. Never raises: the answer has already been recorded."""
    if not configured():
        return {"sent": False, "error": "email not configured (RESEND_API_KEY, NOTIFY_EMAIL)"}
    to = os.environ["NOTIFY_EMAIL"]
    try:
        response = httpx.post(API, timeout=15.0,
                              headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
                              json={"from": os.environ.get("RESEND_FROM", DEFAULT_FROM), "to": [to],
                                    "subject": message["subject"], "text": message["text"], "html": message["html"]})
    except httpx.HTTPError as exc:
        return {"sent": False, "error": f"cannot reach Resend ({exc.__class__.__name__})"}
    if response.status_code >= 300:
        try:
            detail = response.json().get("message", response.text)
        except ValueError:
            detail = response.text
        return {"sent": False, "error": f"Resend {response.status_code}: {detail}"}
    return {"sent": True, "id": response.json().get("id"), "to": to}
