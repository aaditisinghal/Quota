import { useCallback, useEffect, useRef, useState } from "react";
import { api, isMissing, type DemoStep } from "./api";

// Presenter mode: the scripted demo one step at a time. Step 0 is the start; each later step restores the
// server's named checkpoint for it (dispute, routed, April close, May close, end). Next and Back move one step,
// Reset goes back to the start. Keys: → next, ← back, R reset. The step number survives a reload of the page.

const KEY = "quota.presenter.step";

// Where each checkpoint is best seen, unless a step names its own tab.
const CHECKPOINT_TAB: Record<string, string> = {
  start: "case",
  cp_dispute: "case",
  cp_routed: "case",
  cp_april_close: "today",
  cp_may_close: "today",
  cp_end: "today",
};

function saved(): number {
  try {
    return Number(sessionStorage.getItem(KEY) ?? 0) || 0;
  } catch {
    return 0;
  }
}

function save(n: number) {
  try {
    sessionStorage.setItem(KEY, String(n));
  } catch {
    // private window: the step number just isn't kept
  }
}

type Props = { onMoved: (tab?: string) => void; onClose: () => void; paused: boolean };

export function Presenter({ onMoved, onClose, paused }: Props) {
  const [steps, setSteps] = useState<DemoStep[] | null>(null);
  const [missing, setMissing] = useState(false);
  const [current, setCurrent] = useState(saved);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [started, setStarted] = useState<number | null>(null);
  const [now, setNow] = useState(Date.now());
  const busyRef = useRef(false);
  const last = steps && steps.length ? Math.max(...steps.map((s) => s.n)) : 0;

  useEffect(() => {
    api
      .steps()
      .then(setSteps)
      .catch((e) => (isMissing(e) ? setMissing(true) : setError((e as Error).message)));
  }, []);
  useEffect(() => {
    if (started === null) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [started]);

  const go = useCallback(
    async (target: number) => {
      if (busyRef.current || !steps) return;
      const n = Math.max(0, Math.min(last, target));
      busyRef.current = true;
      setBusy(true);
      setError(null);
      try {
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
        save(n);
        onMoved(tab);
      } catch (e) {
        setError(isMissing(e) ? "This server has no presenter steps." : (e as Error).message);
      } finally {
        busyRef.current = false;
        setBusy(false);
      }
    },
    [steps, last, onMoved],
  );

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

  const step = steps?.find((s) => s.n === current) ?? null;
  const next = steps?.find((s) => s.n === current + 1) ?? null;
  const elapsed = started === null ? null : Math.floor((now - started) / 1000);

  return (
    <div className="presenter" role="region" aria-label="Presenter">
      <div className="presenter-controls">
        <button onClick={() => go(current - 1)} disabled={busy || !steps || current === 0} title="Back (←)">
          ← Back
        </button>
        <button className="primary" onClick={() => go(current + 1)} disabled={busy || !steps || current >= last} title="Next (→)">
          {busy ? "…" : "Next →"}
        </button>
        <button onClick={() => go(0)} disabled={busy || !steps} title="Reset (R)">
          Reset
        </button>
      </div>
      <div className="presenter-step">
        <span className="presenter-count">{steps ? `${current} / ${last}` : "–"}</span>
        {elapsed !== null && (
          <span className={`presenter-time ${elapsed > 240 ? "bad" : ""}`} title="Time since the first step (keep it under 4:00)">
            {Math.floor(elapsed / 60)}:{String(elapsed % 60).padStart(2, "0")}
          </span>
        )}
      </div>
      <div className="presenter-narration" aria-live="polite">
        {missing ? (
          <span className="muted">Presenter steps are not available on this server (no /demo/steps).</span>
        ) : error ? (
          <span className="error">{error}</span>
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
          <span className="muted">{steps ? "End of the script." : "Loading the script…"}</span>
        )}
      </div>
      <button className="link presenter-close" onClick={onClose} title="Leave presenter mode">
        ✕
      </button>
    </div>
  );
}
