"""FastAPI over one running world (section 14). Read-only except the three action endpoints: POST /chat,
/inject/parse and /inject/run. The built front end, when present, is served from `static/` at `/`."""

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from quota import clock as clk
from quota.api import chat as chat_turn
from quota.api import inject, views

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


def create_app(session, runs_dir="runs", static_dir=STATIC) -> FastAPI:
    app = FastAPI(title="Quota", version="0.4")
    app.state.session = session
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
                graph = views.trace(session.ro, kind, object_id, as_of, depth)
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
