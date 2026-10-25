import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, isMissing, type DemoStep } from "./api";
import { CFO_SCRIPT, actionsBetween, type CfoServerAction } from "./cfoScript";

// Presenter mode: a scripted demo one step at a time. Step 0 is the start. Next and Back move one step, Reset
// goes back to the start. Keys: → next, ← back, R reset. The script and its step number survive a reload.
//
// Two scripts. "Demo" is the server's: GET /demo/steps lists it, and each step restores a named checkpoint
// (dispute, routed, April close, May close, end). "CFO" is written in cfoScript.ts: each step names a screen,
// and only the steps where the server's state changes run an action there (see actionsBetween).

type Script = "demo" | "cfo";

const KEY = "quota.presenter.step";
const CFO_KEY = "quota.presenter.cfo.step";
const SCRIPT_KEY = "quota.presenter.script";

// Where each checkpoint is best seen, unless a step names its own tab.
const CHECKPOINT_TAB: Record<string, string> = {
  start: "case",
  cp_dispute: "case",
  cp_routed: "case",
  cp_april_close: "today",
  cp_may_close: "today",
  cp_end: "today",
};

function read(key: string): string | null {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string) {
  try {
    sessionStorage.setItem(key, value);
  } catch {
    // private window: it just isn't kept
  }
}

function savedStep(script: Script): number {
  return Number(read(script === "cfo" ? CFO_KEY : KEY) ?? 0) || 0;
}

function savedScript(): Script {
  return read(SCRIPT_KEY) === "cfo" ? "cfo" : "demo";
}

/** Step n of the server's cfo script: POST /demo/step {n, script: "cfo"}. */
async function cfoStep(n: number) {
  const response = await fetch("/demo/step", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ n, script: "cfo" }),
  });
  if (response.ok) return;
  let detail = response.statusText;
  try {
    detail = (await response.json()).detail ?? detail;
  } catch {
    // not JSON
  }
  throw new ApiError(String(detail), response.status);
}

async function runAction(action: CfoServerAction) {
  if ("scenario" in action) await api.runScenario(action.scenario);
  else if ("demoStep" in action) await api.step(action.demoStep);
  else if ("reset" in action) await api.reset();
  else await cfoStep(action.cfoStep);
}

type Props = { onMoved: (tab?: string) => void; onClose: () => void; paused: boolean };

export function Presenter({ onMoved, onClose, paused }: Props) {
  const [script, setScript] = useState<Script>(savedScript);
  const [steps, setSteps] = useState<DemoStep[] | null>(null);
  const [missing, setMissing] = useState(false);
  const [current, setCurrent] = useState(() => savedStep(savedScript()));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [started, setStarted] = useState<number | null>(null);
  const [now, setNow] = useState(Date.now());
  const busyRef = useRef(false);

  // What the bar shows, whichever script is running.
  const shown: DemoStep[] | null = script === "cfo" ? CFO_SCRIPT.map((s, n) => ({ n, narration: s.narration })) : steps;
  const last = shown && shown.length ? Math.max(...shown.map((s) => s.n)) : 0;

  useEffect(() => {
    api
      .steps()
      .then(setSteps)
      .catch((e) => (isMissing(e) ? setMissing(true) : setLoadError((e as Error).message)));
  }, []);
  useEffect(() => {
    if (started === null) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [started]);

  const goDemo = useCallback(
    async (n: number) => {
      if (!steps) return;
      const step = steps.find((s) => s.n === n);
      let tab = step?.tab ?? CHECKPOINT_TAB[step?.checkpoint ?? ""];
      if (n === 0) {
        // A script without a step 0 starts from a plain reset.
        if (step) await api.step(0);
        else await api.reset();
        tab = tab ?? "case";
        setStarted(null);
      } else {
        const ran = await api.step(n);
        tab = ran.tab ?? tab ?? CHECKPOINT_TAB[ran.checkpoint ?? ""];
        setStarted((s) => s ?? Date.now());
      }
      setCurrent(n);
      write(KEY, String(n));
      onMoved(tab);
    },
    [steps, onMoved],
  );

  const goCfo = useCallback(
    async (n: number) => {
      // Reset always restores the start, even from a step that shares its state.
      for (const action of actionsBetween(CFO_SCRIPT, current, n, n === 0)) await runAction(action);
      window.location.hash = CFO_SCRIPT[n].hash;
      if (n === 0) setStarted(null);
      else setStarted((s) => s ?? Date.now());
      setCurrent(n);
      write(CFO_KEY, String(n));
      onMoved();
    },
    [current, onMoved],
  );

  const go = useCallback(
    async (target: number) => {
      if (busyRef.current || (script === "demo" && !steps)) return;
      const n = Math.max(0, Math.min(last, target));
      busyRef.current = true;
      setBusy(true);
      setError(null);
      try {
        if (script === "cfo") await goCfo(n);
        else await goDemo(n);
      } catch (e) {
        setError(isMissing(e) && script === "demo" ? "This server has no presenter steps." : (e as Error).message);
      } finally {
        busyRef.current = false;
        setBusy(false);
      }
    },
    [script, steps, last, goCfo, goDemo],
  );

  const choose = (next: Script) => {
    if (next === script || busyRef.current) return;
    setScript(next);
    write(SCRIPT_KEY, next);
    setCurrent(savedStep(next));
    setStarted(null);
    setError(null);
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (paused || e.metaKey || e.ctrlKey || e.altKey) return;
      const target = e.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) return;
      if (e.key === "ArrowRight") go(current + 1);
      else if (e.key === "ArrowLeft") go(current - 1);
      else if (e.key === "r" || e.key === "R") go(0);
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [go, current, paused]);

  const step = shown?.find((s) => s.n === current) ?? null;
  const next = shown?.find((s) => s.n === current + 1) ?? null;
  const elapsed = started === null ? null : Math.floor((now - started) / 1000);
  const unavailable = script === "demo" && missing;

  return (
    <div className="presenter" role="region" aria-label="Presenter">
      <div className="presenter-controls" role="group" aria-label="Script">
        {(["demo", "cfo"] as const).map((s) => (
          <button
            key={s}
            onClick={() => choose(s)}
            disabled={busy}
            aria-pressed={script === s}
            title={s === "demo" ? "The server's demo script" : "The CFO walkthrough"}
            style={script === s ? { background: "var(--ink)", color: "var(--paper)", borderColor: "var(--ink)" } : undefined}
          >
            {s === "demo" ? "Demo" : "CFO"}
          </button>
        ))}
      </div>
      <div className="presenter-controls">
        <button onClick={() => go(current - 1)} disabled={busy || !shown || current === 0} title="Back (←)">
          ← Back
        </button>
        <button className="primary" onClick={() => go(current + 1)} disabled={busy || !shown || current >= last} title="Next (→)">
          {busy ? "…" : "Next →"}
        </button>
        <button onClick={() => go(0)} disabled={busy || !shown} title="Reset (R)">
          Reset
        </button>
      </div>
      <div className="presenter-step">
        <span className="presenter-count">{shown ? `${current} / ${last}` : "–"}</span>
        {elapsed !== null && (
          <span className={`presenter-time ${elapsed > 240 ? "bad" : ""}`} title="Time since the first step (keep it under 4:00)">
            {Math.floor(elapsed / 60)}:{String(elapsed % 60).padStart(2, "0")}
          </span>
        )}
      </div>
      <div className="presenter-narration" aria-live="polite">
        {unavailable ? (
          <span className="muted">Presenter steps are not available on this server (no /demo/steps).</span>
        ) : error || (script === "demo" && loadError) ? (
          <span className="error">{error ?? loadError}</span>
        ) : step ? (
          <>
            <strong>{step.narration}</strong>
            {next && <span className="muted">Next: {next.narration}</span>}
          </>
        ) : next ? (
          <>
            <strong>The start.</strong>
            <span className="muted">Next: {next.narration}</span>
          </>
        ) : (
          <span className="muted">{shown ? "End of the script." : "Loading the script…"}</span>
        )}
      </div>
      <button className="link presenter-close" onClick={onClose} title="Leave presenter mode">
        ✕
      </button>
    </div>
  );
}
