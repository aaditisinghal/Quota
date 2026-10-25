import { useCallback, useEffect, useRef, useState } from "react";
import { api, isMissing, type DemoStep } from "./api";

// Presenter mode: the scripted demo one step at a time. Next runs step n+1 on the server (which restores the
// checkpoint before it first), Back runs step n-1 (or resets before step 1), Reset goes back to the start.
// Keys: → next, ← back, R reset. The step number survives a reload of the page.

const KEY = "quota.presenter.step";

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
      const n = Math.max(0, Math.min(steps.length, target));
      busyRef.current = true;
      setBusy(true);
      setError(null);
      try {
        let tab: string | undefined;
        if (n === 0) {
          await api.reset();
          setStarted(null);
        } else {
          const ran = await api.step(n);
          tab = ran.tab ?? steps.find((s) => s.n === n)?.tab;
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
    [steps, onMoved],
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

  const total = steps?.length ?? 0;
  const step = steps?.find((s) => s.n === current) ?? null;
  const next = steps?.find((s) => s.n === current + 1) ?? null;
  const elapsed = started === null ? null : Math.floor((now - started) / 1000);

  return (
    <div className="presenter" role="region" aria-label="Presenter">
      <div className="presenter-controls">
        <button onClick={() => go(current - 1)} disabled={busy || !steps || current === 0} title="Back (←)">
          ← Back
        </button>
        <button className="primary" onClick={() => go(current + 1)} disabled={busy || !steps || current >= total} title="Next (→)">
          {busy ? "…" : "Next →"}
        </button>
        <button onClick={() => go(0)} disabled={busy || !steps} title="Reset (R)">
          Reset
        </button>
      </div>
      <div className="presenter-step">
        <span className="presenter-count">{steps ? `${current} / ${total}` : "–"}</span>
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
            <strong>The start: Apr 27, 10:00 AM PT.</strong>
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
