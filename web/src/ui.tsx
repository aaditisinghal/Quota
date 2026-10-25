import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { isMissing, type Band, type Confidence } from "./api";

// Small pieces the CFO screens share: a side drawer, the confidence badge and factors, a "simulated" label,
// and loading a call that a server may not have yet.

/** Load `fetcher` whenever `deps` change; `missing` when the server has no such endpoint (404/405). */
export function useApi<T>(fetcher: () => Promise<T>, deps: unknown[]): { data: T | null; missing: boolean; error: string | null } {
  const [data, setData] = useState<T | null>(null);
  const [missing, setMissing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    fetcher()
      .then((d) => {
        if (!live) return;
        setData(d);
        setMissing(false);
        setError(null);
      })
      .catch((e) => {
        if (!live) return;
        if (isMissing(e)) setMissing(true);
        else setError((e as Error).message);
      });
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, missing, error };
}

export function Unavailable({ what, path }: { what: string; path: string }) {
  return (
    <div className="card unknown state-card" role="status">
      <span className="state-icon" aria-hidden="true">
        ?
      </span>
      <div>
        <strong className="state-title">{what} is not available on this server yet</strong>
        <p className="muted">
          The screen reads <code>GET {path}</code>, which this server does not answer.
        </p>
      </div>
    </div>
  );
}

/** A call that failed: what it was for, and the server's own words. */
export function ErrorState({ what, message }: { what: string; message: string }) {
  return (
    <div className="state-card is-error" role="alert">
      <span className="state-icon" aria-hidden="true">
        !
      </span>
      <div>
        <strong className="state-title">Could not load {what}</strong>
        <p>{message}</p>
      </div>
    </div>
  );
}

/** Nothing to show, said plainly, in the same card shape as the other states. */
export function EmptyState({ children }: { children: ReactNode }) {
  return <p className="empty-state">{children}</p>;
}

/** Grey lines in the shape of what is coming, so the screen does not jump when it arrives. */
export function Skeleton({ label, rows = 4, table, heading = true }: { label: string; rows?: number; table?: boolean; heading?: boolean }) {
  return (
    <div className={`skeleton ${table ? "sk-table" : ""}`} role="status" aria-live="polite" aria-busy="true">
      <span className="sr-only">{label}</span>
      {heading && <span className="sk-line sk-head" aria-hidden="true" />}
      {Array.from({ length: rows }, (_, i) => (
        <span key={i} className="sk-line" aria-hidden="true" />
      ))}
    </div>
  );
}

export function Simulated({ children }: { children?: ReactNode }) {
  return (
    <span className="tag simulated" title="Not from a source system: generated for the demo">
      simulated{children ? <> · {children}</> : null}
    </span>
  );
}

const BAND_TEXT: Record<Band, string> = { cleared: "cleared", note: "cleared with note", held: "held" };

/** The score and its band in words, never colour alone. Held needs a person, so it carries the accent. */
export function ConfidenceBadge({ score, band, href }: { score: number | null | undefined; band?: Band | string | null; href?: string }) {
  if (score === null || score === undefined) return null;
  const b = (band ?? "cleared") as Band;
  const body = (
    <>
      <span className="conf-score">{Math.round(score)}</span>
      <span className="conf-band">
        {b === "held" ? "! " : b === "note" ? "· " : "✓ "}
        {BAND_TEXT[b] ?? b}
      </span>
    </>
  );
  return href ? (
    <a className={`conf conf-${b}`} href={href} title="Confidence: the weakest of six evidence factors">
      {body}
    </a>
  ) : (
    <span className={`conf conf-${b}`} title="Confidence: the weakest of six evidence factors">
      {body}
    </span>
  );
}

/** The six factors; the weakest one sets the score, so it is marked. */
export function Factors({ confidence }: { confidence: Confidence }) {
  const weakest = Math.min(...confidence.factors.map((f) => f.value));
  return (
    <table className="table factors">
      <tbody>
        {confidence.factors.map((f) => (
          <tr key={f.name} className={f.value === weakest && weakest < 1 ? "changed" : ""}>
            <td>{f.name.replace(/_/g, " ")}</td>
            <td className="num">{f.value.toFixed(2)}</td>
            <td className="muted">
              {f.reason}
              {f.value === weakest && weakest < 1 && <strong> · weakest, sets the score</strong>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const CLOSE_MS = 180;
const reducedMotion = () => typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/** A panel that slides in from the right; closing slides it out, then goes back to `back` (the hash without the drawer). */
export function Drawer({ title, back, children, wide }: { title: ReactNode; back: string; children: ReactNode; wide?: boolean }) {
  const [closing, setClosing] = useState(false);
  const panel = useRef<HTMLElement>(null);
  const titleId = useId();
  const timer = useRef<number | undefined>(undefined);

  const close = useCallback(() => {
    if (timer.current !== undefined) return;
    if (reducedMotion()) {
      window.location.hash = back;
      return;
    }
    setClosing(true);
    timer.current = window.setTimeout(() => {
      timer.current = undefined;
      window.location.hash = back;
      setClosing(false); // when the same drawer stays mounted (the hash did not unmount it)
    }, CLOSE_MS);
  }, [back]);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  useEffect(() => {
    // keyboard users land inside the drawer; focus goes back where it was when it closes
    const before = document.activeElement as HTMLElement | null;
    panel.current?.focus({ preventScroll: true });
    return () => {
      if (before && document.contains(before)) before.focus({ preventScroll: true });
    };
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [close]);

  return (
    <div className={`drawer-wrap ${closing ? "closing" : ""}`} onMouseDown={(e) => e.target === e.currentTarget && close()}>
      <aside ref={panel} className={`drawer ${wide ? "wide" : ""}`} role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1}>
        <div className="drawer-head">
          <h2 id={titleId}>{title}</h2>
          <a
            className="drawer-close"
            href={`#${back}`}
            aria-label="Close"
            title="Close (Esc)"
            onClick={(e) => {
              if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
              e.preventDefault();
              close();
            }}
          >
            ✕
          </a>
        </div>
        <div className="drawer-body">{children}</div>
      </aside>
    </div>
  );
}

export function pct(bps: number | null | undefined): string {
  return bps === null || bps === undefined ? "" : `${(bps / 100).toFixed(2)}%`;
}
