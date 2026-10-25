"""FastAPI over one running world (section 14). Read-only except the three action endpoints: POST /chat,
/inject/parse and /inject/run. The built front end, when present, is served from `static/` at `/`."""

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from quota import clock as clk
from quota.api import chat as chat_turn
from quota.api import inject, scenarios, views
from quota.sim import checkpoints

STATIC = Path(__file__).with_name("static")


def scorecard(seed: int, runs_dir: Path) -> dict:
    """The eval's own output for this seed: runs/seed-N/scorecard.json, produced by running the eval once if
    it is not there yet (about 20 seconds without Stripe)."""
    path = runs_dir / f"seed-{seed}" / "scorecard.json"
    if not path.exists():
        from quota.eval import run_eval
        run_eval(seed, out_dir=path.parent)
    return {"seed": seed, "source": str(path), **json.loads(path.read_text(encoding="utf-8"))}


class ChatIn(BaseModel):
    as_principal: str
    text: str
    to: str | None = None


class ParseIn(BaseModel):
    text: str


class RunIn(BaseModel):
    events: list[dict]


class StepIn(BaseModel):
    n: int
    script: str = "default"


class ReviewIn(BaseModel):
    resolution: dict


class AnswerIn(BaseModel):
    as_principal: str
    decision: str = "approve"          # approve | reject | reply
    text: str | None = None


class AuditorIn(BaseModel):
    message: str
    conversation_id: str | None = None


# The guided demo (R4): the start, then each checkpoint of the scripted story, with what to say there.
DEMO_STEPS = [
    ("start", "Monday 2026-04-27, just after the April commission run: Acme is booked in Q2 at $12,000."),
    ("cp_dispute", "Jordan disputes the booking date, is held twice on the evidence, and escalates."),
    ("cp_routed", "Marcus grants the Q1 exception; $6,000 is over his limit, so A-0002 goes to Priya."),
    ("cp_april_close", "April closes: Acme counts in Q1 at $18,000, accrued and capitalized over 36 months."),
    ("cp_may_close", "May payroll paid Jordan; two months of amortization are posted."),
    ("cp_end", "Acme churns June 30: refund, clawback to 1290, and every invariant still holds."),
]
# DEMO_FLOW D6: the CFO's morning. The UI owns routes and narration on screen; the server only restores state.
# cfo_morning is the served Acme world (with its cast) on 2026-05-15: Acme's Q1 exception waits on Priya, and
# Riley's side letter holds Summit out of May payroll.
CFO_STEPS = [
    ("cfo_morning", "Home: every input, every rep, and what needs the CFO this morning."),
    ("cfo_morning", "Reps board: Jordan."),
    ("cfo_morning", "Acme contract: the calibration panel (80, cleared with a note) and Jordan's dispute."),
    ("cfo_morning", "Orbit: the split, Alex's and Riley's views."),
    ("cfo_morning", "Needs you: Summit's side letter for Priya, and the Acme exception chain."),
    ("cfo_morning", "The answers clear the inbox."),
    ("cp_end", "June 30: the ledger health cascade after Acme churns."),
    ("cp_may_close", "Birch churns (run the birch_churns scenario from here)."),
    ("cp_end", "Needs you: 0. Finance sorted."),
]
DEMO_SCRIPTS = {"default": DEMO_STEPS, "cfo": CFO_STEPS}

def create_app(session, runs_dir="runs", static_dir=STATIC) -> FastAPI:
    app = FastAPI(title="Quota", version="0.4")
    app.state.session = session
    from quota.search.mirror import Mirror
    mirror = Mirror()

    @app.middleware("http")
    async def mirror_after_actions(request, call_next):
        """E2: after every action, push new decisions to the Elastic mirror (a no-op when it is off)."""
        response = await call_next(request)
        if request.method == "POST":
            with session.lock:
                mirror(session.ro)
        return response
    runs = Path(runs_dir)

    @app.get("/state")
    def state():
        with session.lock:
            return session.state()

    @app.get("/timeline")
    def timeline(since: int = Query(0, ge=0)):
        with session.lock:
            return {"clock": session.world.clock.now(), "items": views.timeline(session.world, since)}

    @app.get("/trace/{kind}/{object_id}")
    def trace(kind: str, object_id: str, as_of: str | None = None, depth: int = Query(3, ge=0, le=6)):
        if as_of is not None:
            if not inject._timestamp(as_of):
                raise HTTPException(422, f"as_of {as_of!r} is not a date or timestamp")
            as_of = clk.end_of_day(as_of)            # a bare date means the end of that day
        with session.lock:
            try:
                graph = views.trace(session.ro, kind, object_id, as_of, depth, session.provenance)
            except KeyError as exc:
                raise HTTPException(404, str(exc))
        if not graph["known"] and as_of is None:
            raise HTTPException(404, f"no {kind} {object_id}")
        return graph

    @app.get("/trace/{object_id}")
    def trace_by_id(object_id: str, as_of: str | None = None, depth: int = Query(3, ge=0, le=6)):
        kind = views.kind_of(object_id)
        if kind is None:
            raise HTTPException(404, f"cannot tell what {object_id} is")
        return trace(kind, object_id, as_of, depth)

    @app.get("/ledger/health")
    def health():
        with session.lock:
            return views.health(session.world)

    @app.get("/today")
    def today():
        with session.lock:
            return views.today(session.world)

    @app.get("/messages")
    def messages(principal: str | None = None):
        with session.lock:
            return session.messages(principal)

    @app.get("/statement/{rep_id}")
    def get_statement(rep_id: str, as_principal: str | None = None):
        """A rep sees only their own statement (SPEC: reps cannot see other reps' pay); people in finance and
        Sales Ops can see anyone's."""
        with session.lock:
            people = {p["id"]: p for p in session.principals()}
            if rep_id not in people:
                raise HTTPException(404, f"no person {rep_id}")
            viewer = people.get(as_principal or "")
            if viewer is not None and "dispute:own_lines" in viewer["capabilities"] and as_principal != rep_id:
                raise HTTPException(403, "a rep can see only their own statement")
            return views.statement(session.world, rep_id)

    @app.get("/scorecard")
    def get_scorecard(seed: int = 7):
        return scorecard(seed, runs)

    @app.post("/chat")
    def post_chat(body: ChatIn):
        with session.lock:
            try:
                return chat_turn.chat(session, body.as_principal, body.text, body.to)
            except ValueError as exc:
                raise HTTPException(422, str(exc))

    @app.post("/inject/parse")
    def post_parse(body: ParseIn):
        with session.lock:
            return inject.parse(session, body.text)

    def scenario_menu():
        return [s.card() for s in scenarios.SCENARIOS]

    def scenario_run(name: str):
        if name not in scenarios.BY_NAME:
            raise HTTPException(404, f"no scenario {name!r}; try {sorted(scenarios.BY_NAME)}")
        with session.lock:
            demo, result = scenarios.run(name, session.checkpoint_dir)
            session.swap(demo.world, name)
            return result

    def demo_reset():
        if session.restart is None:
            raise HTTPException(400, f"the {session.name} world has no start to restore")
        with session.lock:
            session.swap(session.restart(), None)
            return {"clock": session.world.clock.now(), "timeline": views.timeline(session.world, 0)}

    def demo_steps(script: str = "default"):
        if script not in DEMO_SCRIPTS:
            raise HTTPException(404, f"no presenter script {script!r}; try {sorted(DEMO_SCRIPTS)}")
        return [{"n": n, "checkpoint": cp, "narration": text} for n, (cp, text) in enumerate(DEMO_SCRIPTS[script])]

    def demo_step(body: StepIn):
        steps = DEMO_SCRIPTS.get(body.script)
        if steps is None:
            raise HTTPException(404, f"no presenter script {body.script!r}")
        if not 0 <= body.n < len(steps):
            raise HTTPException(400, f"steps are 0..{len(steps) - 1}")
        checkpoint, narration = steps[body.n]
        with session.lock:
            if checkpoint == "cfo_morning":
                if session.scenario != "cfo_morning":      # one build serves every morning step
                    from quota.search import auditor_world
                    session.swap(auditor_world.build().world, "cfo_morning")
            elif checkpoint == "start":
                demo_reset()
            else:
                session.poll_findings()                    # E6: once at each demo step
                checkpoints.ensure_snapshots(session.checkpoint_dir)
                session.swap(checkpoints.restore(checkpoint, directory=session.checkpoint_dir).world, checkpoint)
            return {"n": body.n, "script": body.script, "checkpoint": checkpoint, "narration": narration,
                    "clock": session.world.clock.now(), "timeline": views.timeline(session.world, 0)}

    def auditor(body: AuditorIn):
        """E6: one turn with the auditor agent in Agent Builder (read only; it never changes the ledger)."""
        from quota.search import agent_setup, elastic_client
        if not elastic_client.configured():
            raise HTTPException(503, "Elastic is not configured: set ELASTICSEARCH_URL and ELASTIC_API_KEY in .env")
        try:
            return agent_setup.converse(elastic_client.kibana, body.message, body.conversation_id)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}

    def elastic_verify():
        from quota.search import elastic_client
        from quota.search import mirror as mirroring
        with session.lock:
            local = mirroring.head(mirroring.documents(session.ro))
            if not elastic_client.configured():
                return {"ok": None, "ledger": local, "mirror": None, "line": "Elastic is not configured"}
            try:
                return mirroring.verify(session.ro, mirror.client or elastic_client.es())
            except Exception as exc:
                return {"ok": False, "ledger": local, "mirror": None,
                        "line": f"mirror unreachable: {type(exc).__name__}: {str(exc)[:200]}"}

    # ---------- DEMO_FLOW D1-D5: the CFO flow's read model ----------

    def flow():
        from quota.api.demo_flow import Flow
        return Flow(session)

    def found(value, what):
        if value is None:
            raise HTTPException(404, f"no {what}")
        return value

    def line_confidence(line_id: str):
        with session.lock:
            return found(flow().confidence(line_id), f"commission line {line_id}")

    def line_calibration(line_id: str):
        with session.lock:
            return found(flow().calibration(line_id), f"commission line {line_id}")

    def sources_rail():
        with session.lock:
            return flow().sources()

    def source_rows(kind: str, q: str | None = None, limit: int = Query(200, ge=1, le=5000)):
        if kind not in ("contracts", "messages", "crm", "hr", "stripe", "payouts"):
            raise HTTPException(404, f"no source {kind!r}")
        with session.lock:
            return flow().source_rows(kind, q, limit)

    def reps_board():
        with session.lock:
            return flow().reps()

    def rep_view(rep_id: str):
        with session.lock:
            return found(flow().rep_detail(rep_id), f"rep {rep_id}")

    def contract_view(deal_id: str, viewer: str | None = None):
        with session.lock:
            return found(flow().contract(deal_id, viewer), f"contract for {deal_id}")

    def needs_you():
        with session.lock:
            return flow().needs_you()

    def needs_you_answer(item_id: str, body: AnswerIn):
        with session.lock:
            return flow().answer(item_id, body.as_principal, body.decision, body.text)

    def home_kpis():
        with session.lock:
            return flow().home()

    def intake_view():
        if session.intake is None:
            raise HTTPException(404, "no inbox attached; serve with --from-inbox (or run python -m quota intake inbox/)")
        report, result = session.intake["report"], session.intake["backtest"]
        return {"inbox": session.inbox,
                "files": [{"file": f["file"], "kind": f["kind"], "rows": f["rows"], "loaded": f.get("loaded"),
                           "mapping_source": f.get("mapping_source"), "mappings": f.get("mappings", [])}
                          for f in report["files"]],
                "loaded": report["loaded"], "review_items": report["review_items"], "log": report["log"],
                "plans": report["plans"],
                "backtest": {k: result[k] for k in ("quarter", "rows", "matched", "errors", "unexplained",
                                                    "overpaid_cents", "headline", "findings")}}

    def review_resolve(item_id: str, body: ReviewIn):
        if session.intake is None:
            raise HTTPException(404, "no inbox attached")
        from quota.intake.intake import resolve
        with session.lock:
            if item_id not in {i["id"] for i in session.intake["report"]["review_items"]}:
                raise HTTPException(404, f"no open review item {item_id}")
            resolve(session.inbox, item_id, body.resolution)
            session.attach_inbox(session.inbox)
            return {"resolved": item_id, **intake_view()}

    for prefix in ("/api", ""):        # NEXT_TASKS names /api/...; the UI calls the root paths like the rest
        app.get(f"{prefix}/intake")(intake_view)
        app.get(f"{prefix}/lines/{{line_id}}/confidence")(line_confidence)
        app.get(f"{prefix}/lines/{{line_id}}/calibration")(line_calibration)
        app.get(f"{prefix}/sources")(sources_rail)
        app.get(f"{prefix}/sources/{{kind}}/rows")(source_rows)
        app.get(f"{prefix}/reps")(reps_board)
        app.get(f"{prefix}/reps/{{rep_id}}")(rep_view)
        app.get(f"{prefix}/contracts/{{deal_id}}")(contract_view)
        app.get(f"{prefix}/needs-you")(needs_you)
        app.post(f"{prefix}/needs-you/{{item_id}}/answer")(needs_you_answer)
        app.get(f"{prefix}/home")(home_kpis)
        app.get(f"{prefix}/elastic/verify")(elastic_verify)
        app.post(f"{prefix}/auditor")(auditor)
        app.post(f"{prefix}/review/{{item_id}}")(review_resolve)
        app.get(f"{prefix}/scenarios")(scenario_menu)
        app.post(f"{prefix}/scenarios/{{name}}/run")(scenario_run)
        app.post(f"{prefix}/demo/reset")(demo_reset)
        app.get(f"{prefix}/demo/steps")(demo_steps)
        app.post(f"{prefix}/demo/step")(demo_step)

    @app.post("/inject/run")
    def post_run(body: RunIn):
        with session.lock:
            try:
                return inject.run(session, body.events)
            except inject.InjectError as exc:
                raise HTTPException(422, str(exc))

    if Path(static_dir).joinpath("index.html").exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app
