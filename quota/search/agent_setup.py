"""`python -m quota elastic agent-setup` (ELASTIC_TASKS E6): the read-only auditor agent in Agent Builder, its
tools, and the promise-sweep Workflow. Safe to re-run: like Elastic's starter (setup_agent.py), each tool and the
agent is fetched, then updated with PUT or created with POST."""

import copy
from pathlib import Path

WORKFLOW_PATH = Path(__file__).resolve().parents[2] / "elastic" / "workflows" / "promise-sweep.yaml"
AGENT_ID = "quota-auditor"

TOOLS = [
    {"id": "quota_decisions_search", "type": "index_search",
     "description": "Search Quota's decision log mirror: commission decisions, approvals, precedents and refusals, "
                    "with explanations. Use to find what Quota decided about a rep, deal or account and why. Cite "
                    "decision ids.",
     "configuration": {"pattern": "quota-decisions"}},
    {"id": "quota_comms_search", "type": "index_search",
     "description": "Search sales emails and Slack messages about deals. Use to find what a rep told a customer. "
                    "Cite doc_id with source_file and source_row.",
     "configuration": {"pattern": "quota-comms"}},
    {"id": "quota_exceptions_by_approver", "type": "esql",
     "description": "Exceptions an approver decided, with dollars, by outcome and rep. Use for 'what did <approver> "
                    "approve'.",
     "configuration": {"query": "FROM quota-decisions | WHERE kind == \"approval\" AND approver == ?approver | STATS "
                                "n = COUNT(*), dollars = SUM(amount_cents) / 100 BY outcome, rep_id | SORT dollars "
                                "DESC | LIMIT 50",
                       "params": {"approver": {"type": "string", "description": "Principal id, e.g. marcus.lee"}}}},
    {"id": "quota_held_by_reason", "type": "esql",
     "description": "Held commission dollars and counts by reason code. Use for 'what is held and why'.",
     "configuration": {"query": "FROM quota-decisions | WHERE outcome == \"held\" | STATS n = COUNT(*), dollars = "
                                "SUM(amount_cents) / 100 BY reason_code | SORT dollars DESC", "params": {}}},
    {"id": "quota_repeat_exceptions", "type": "esql",
     "description": "Reps with two or more exceptions in a quarter, and who approved them. Use for control testing "
                    "and 'any pattern of exceptions'.",
     "configuration": {"query": "FROM quota-decisions | WHERE kind == \"exception\" AND quarter == ?quarter | STATS "
                                "n = COUNT(*) BY rep_id, approver | WHERE n >= 2 | SORT n DESC",
                       "params": {"quarter": {"type": "string", "description": "e.g. 2026Q2"}}}},
    {"id": "quota_refusals_by_code", "type": "esql",
     "description": "Write-path refusals by reason code and actor. Use for 'did anyone try to bypass controls'.",
     "configuration": {"query": "FROM quota-decisions | WHERE kind == \"refusal\" | STATS n = COUNT(*) BY "
                                "reason_code, seat | SORT n DESC", "params": {}}},
    {"id": "quota_run_promise_sweep", "type": "workflow",
     "description": "Run the off-contract promise sweep now over recent messages and save findings for Quota. Use "
                    "only when asked to sweep or re-check.",
     "configuration": {"workflow_id": None}},
]

AGENT = {
    "id": AGENT_ID, "name": "Quota auditor", "labels": ["calhacks", "quota"],
    "description": "Read-only auditor assistant over Quota's hash-verified decision log and sales messages.",
    "avatar_color": "#5B8DEF", "avatar_symbol": "QA",
    "configuration": {
        "instructions": (
            "You are Quota's auditor assistant for the Controller and external auditors. Read only: you never "
            "approve, post or change anything.\n"
            "- Ground every statement in tool results. Cite decision ids (D-..., A-..., P-..., R-...) and message "
            "doc_ids with source_file and row.\n"
            "- Quota's SQLite ledger is the system of record; quota-decisions is a mirror verified by chain hash. Say "
            "so when asked about reliability.\n"
            "- For dollar or pattern questions use the ES|QL tools; for 'why' questions, search decisions, then "
            "messages.\n"
            "- Keep answers to tight bullets."),
        "tools": [{"tool_ids": [t["id"] for t in TOOLS]}],
    },
}

REHEARSAL = ["What did Marcus approve this half, and was any of it over his limit?",
             "Why is Riley's Summit commission held? Show the evidence.",
             "Did anyone try to bypass a control?",
             "Any rep with repeated exceptions in 2026Q2?"]


def upsert(kibana, kind: str, body: dict, out=print) -> bool:
    r = kibana("GET", f"/api/agent_builder/{kind}/{body['id']}")
    if r.ok:
        update = {k: v for k, v in body.items() if k not in ("id", "type", "name")}
        r, verb = kibana("PUT", f"/api/agent_builder/{kind}/{body['id']}", json=update), "updated"
    else:
        r, verb = kibana("POST", f"/api/agent_builder/{kind}", json=body), "created"
    out(f"  {kind[:-1]} {body['id']}: {verb if r.ok else 'FAILED'}" + ("" if r.ok else f" {r.status_code} {r.text[:300]}"))
    return r.ok


def upsert_workflow(kibana, out=print) -> str | None:
    yaml_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    existing = kibana("GET", "/api/workflows", params={"query": "promise-sweep", "size": 100})
    if existing.ok:
        for wf in existing.json().get("results", []):
            if wf.get("name") == "promise-sweep":
                r = kibana("PUT", f"/api/workflows/workflow/{wf['id']}", json={"yaml": yaml_text})
                out(f"  workflow promise-sweep: updated ({r.status_code})")
                return wf["id"]
    r = None
    for path in ("/api/workflows/workflow", "/api/workflows"):      # newer Kibana, then older
        r = kibana("POST", path, json={"yaml": yaml_text})
        if r.ok:
            out("  workflow promise-sweep: created")
            return r.json().get("id")
    out(f"  workflow promise-sweep: FAILED {r.status_code} {r.text[:300]}")
    out("  (Workflows may need enabling: Stack Management > Advanced Settings > workflows:ui:enabled)")
    return None


def setup(kibana, out=print) -> dict:
    """Upload the workflow, then the tools (the workflow tool only if the upload worked), then the agent."""
    tools, agent = copy.deepcopy(TOOLS), copy.deepcopy(AGENT)
    workflow_id = upsert_workflow(kibana, out)
    for tool in tools:
        if tool["type"] == "workflow":
            if not workflow_id:
                agent["configuration"]["tools"][0]["tool_ids"].remove(tool["id"])
                out(f"  tool {tool['id']}: skipped (no workflow)")
                continue
            tool["configuration"]["workflow_id"] = workflow_id
        upsert(kibana, "tools", tool, out)
    ok = upsert(kibana, "agents", agent, out)
    smoke = kibana("POST", "/api/agent_builder/tools/_execute", json={"tool_id": "quota_held_by_reason", "tool_params": {}})
    out(f"  smoke test quota_held_by_reason: {smoke.status_code}")
    return {"workflow_id": workflow_id, "agent": ok, "smoke": smoke.status_code}


def converse(kibana, message: str, conversation_id: str | None = None) -> dict:
    """POST /api/auditor: one turn with the auditor agent (parsed as the starter's /api/chat does)."""
    payload = {"input": message, "agent_id": AGENT_ID}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    r = kibana("POST", "/api/agent_builder/converse", json=payload)
    if not r.ok:
        return {"error": f"{r.status_code}: {r.text[:500]}"}
    return parse_converse(r.json())


def parse_converse(data: dict) -> dict:
    return {"conversation_id": data.get("conversation_id"),
            "message": data.get("response", {}).get("message", ""),
            "tools": [{"tool": s.get("tool_id"), "params": s.get("params")} for s in data.get("steps", [])
                      if s.get("type") == "tool_call"]}
