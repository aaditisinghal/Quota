"""DEMO_FLOW D7: the `cfo` presenter path end to end in a real browser, offline.

Serves the Acme world (no API keys, no network) with uvicorn on a free port, opens `/?present`, picks the CFO
script, presses Reset, then Next until Next is disabled. After every step: no uncaught page errors, a non-empty
narration line, and the hash on the step's route. Skips cleanly when Playwright or its Chromium is missing, when
the screens are not built, or when the built page has no CFO script control yet.
"""

import re
import socket
import threading
import time
from pathlib import Path
from urllib.parse import unquote

import pytest

from quota.api.app import STATIC, create_app
from quota.api.session import acme_session

sync_api = pytest.importorskip("playwright.sync_api", reason="Playwright is not installed (pip install playwright)")

# The CFO script's screens, in order (web/src/cfoScript.ts). Step 0 is where Reset lands.
ROUTES = [
    "home",
    "home/source/contracts",
    "home/source/messages",
    "reps",
    "rep/jordan.kim",
    "rep/jordan.kim/acme/calibration",
    "rep/alex.chen/orbit",
    "rep/riley.shah/orbit",
    "rep/alex.chen/orbit+v2",
    "home",
    "today",
    "today",
    "trace/CL-0002",
    "inject",
    "home",
]
STEP_TIMEOUT_MS = 180_000          # a step may build the checkpoint snapshots or run a scenario on first use


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    if not Path(STATIC, "index.html").exists():
        pytest.skip("the screens are not built (cd web && npm run build)")
    import uvicorn
    session = acme_session()
    session.checkpoint_dir = tmp_path_factory.mktemp("checkpoints")
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(create_app(session), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not srv.started:
        if time.time() > deadline or not thread.is_alive():
            pytest.fail("uvicorn did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.should_exit = True
        thread.join(timeout=10)
        session.close()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:                     # no Chromium downloaded: never fetch one here
            pytest.skip(f"Playwright's Chromium is not available ({str(exc).splitlines()[0]})")
        try:
            yield b
        finally:
            b.close()


def _offline(context, base):
    """Abort every request that is not to the local server."""
    context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base)
                  or route.request.url.startswith("data:") else route.abort())


def _hash_route(page) -> str:
    return unquote(page.evaluate("location.hash").lstrip("#")).split("?")[0]


def _choose_cfo(page) -> bool:
    """Pick the CFO script in the presenter bar: a <select> option or a button labelled CFO."""
    bar = page.locator(".presenter")
    for select in bar.locator("select").all():
        labels = select.locator("option").all_inner_texts()
        label = next((t for t in labels if re.search(r"\bCFO\b", t, re.I)), None)
        if label is not None:
            select.select_option(label=label)
            return True
    button = bar.get_by_role("button", name=re.compile(r"\bCFO\b", re.I))
    if button.count():
        button.first.click()
        return True
    tab = bar.get_by_role("tab", name=re.compile(r"\bCFO\b", re.I))
    if tab.count():
        tab.first.click()
        return True
    return False


def _wait_for_step(page, n: int, route: str, errors: list):
    """Wait until the presenter has settled on step n with the hash on `route`, then check the step."""
    page.wait_for_function(
        """([n, route]) => {
            const hash = decodeURIComponent(location.hash.slice(1)).split("?")[0];
            const count = document.querySelector(".presenter-count");
            const next = document.querySelector(".presenter-controls button.primary");
            const settled = !next || !/…/.test(next.textContent || "");
            const counted = !count || new RegExp("^\\\\s*" + n + "\\\\s*/").test(count.textContent || "");
            return hash === route && counted && settled;
        }""",
        arg=[n, route], timeout=STEP_TIMEOUT_MS)
    assert not errors, f"step {n} ({route}): uncaught page errors {errors}"
    narration = page.locator(".presenter-narration strong")
    narration.first.wait_for(timeout=10_000)
    assert narration.first.inner_text().strip(), f"step {n} ({route}): the narration line is empty"
    assert _hash_route(page) == route


def test_the_cfo_presenter_path_end_to_end(server, browser):
    import httpx
    steps = httpx.get(f"{server}/demo/steps", params={"script": "cfo"}, timeout=30)
    assert steps.status_code == 200, f"GET /demo/steps?script=cfo: {steps.status_code} {steps.text[:200]}"

    context = browser.new_context(viewport={"width": 1440, "height": 900})
    _offline(context, server)
    page = context.new_page()
    page.set_default_timeout(30_000)
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        page.goto(f"{server}/?present")
        page.locator(".presenter").wait_for()
        page.locator(".presenter-controls button.primary").wait_for()
        if not _choose_cfo(page):
            pytest.skip("GET /demo/steps works but the built page has no CFO script control in the presenter bar "
                        "yet (rebuild web/ once Presenter.tsx offers the cfo script)")

        controls = page.locator(".presenter-controls")
        controls.get_by_role("button", name=re.compile(r"Reset", re.I)).click()
        _wait_for_step(page, 0, ROUTES[0], errors)

        next_button = controls.locator("button.primary")
        saw_cash = False
        n = 0
        while next_button.is_enabled():
            n += 1
            assert n < len(ROUTES), f"Next is still enabled after the last expected step ({ROUTES[-1]})"
            next_button.click()
            _wait_for_step(page, n, ROUTES[n], errors)
            if ROUTES[n] == "today":
                page.get_by_text(re.compile(r"\bCash\b")).first.wait_for(timeout=STEP_TIMEOUT_MS)
                saw_cash = True
        assert n == len(ROUTES) - 1, f"the script stopped at step {n} ({ROUTES[n]}), expected {len(ROUTES) - 1}"
        assert saw_cash, "the trial balance (today) never showed Cash"

        # The close: Needs you reads 0 when the server has the endpoint, else the "not available" card.
        if httpx.get(f"{server}/api/needs-you", timeout=30).status_code == 200:
            total = page.locator(".needs-you .total").first
            total.wait_for(timeout=STEP_TIMEOUT_MS)
            page.wait_for_function(
                "() => (document.querySelector('.needs-you .total')?.textContent || '').trim() === '0'",
                timeout=30_000)
            assert total.inner_text().strip() == "0"
        else:
            page.get_by_text("is not available on this server yet").first.wait_for()
        assert not errors, f"uncaught page errors: {errors}"
    finally:
        context.close()
