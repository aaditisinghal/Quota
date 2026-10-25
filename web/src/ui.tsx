import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { isMissing, type Band, type Confidence } from "./api";

// Small pieces the CFO screens share: a side drawer, the confidence badge and factors, a "simulated" label,
// and loading a call that a server may not have yet.

/** Load `fetcher` whenever `deps` change; `missing` when the server has no such endpoint (404/405). */
export function useApi<T>(
  fetcher: () => Promise<T>,
  deps: unknown[],
): { data: T | null; missing: boolean; error: string | null; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [missing, setMissing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const reload = useCallback(() => {
    setError(null);
    setAttempt((n) => n + 1);
  }, []);
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
  }, [...deps, attempt]);
  return { data, missing, error, reload };
}

/** A friendly card for a screen the server cannot answer yet. */
export function Unavailable({ what, path }: { what: string; path: string }) {
  return (
    <StateCard icon="?" title={`${what} is not available on this server yet`}>
      <p>
        This part reads <code>GET {path}</code>, which this server does not answer. Everything else keeps working.
      </p>
    </StateCard>
  );
}

/** A call that failed: what we tried to load, the server's words, and a way to try again. */
export function ErrorNote({ what, error, onRetry }: { what: string; error: string; onRetry?: () => void }) {
  return (
    <StateCard tone="error" icon="!" title={`Could not load ${what}`} role="alert">
      <p>{error}</p>
      {onRetry && (
        <div className="state-actions">
          <button onClick={onRetry}>Try again</button>
        </div>
      )}
    </StateCard>
  );
}

/** Nothing to show, said plainly. */
export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <StateCard icon="–" title={title} compact>
      {children}
    </StateCard>
  );
}

function StateCard({ icon, title, tone, compact, role, children }: {
  icon: string;
  title: string;
  tone?: "error";
  compact?: boolean;
  role?: string;
  children?: ReactNode;
}) {
  return (
    <div className={`state-card ${tone ?? ""} ${compact ? "compact" : ""}`} role={role}>
      <span className="state-icon" aria-hidden="true">
        {icon}
      </span>
      <div className="state-body">
        <h3 className="state-title">{title}</h3>
        {children}
      </div>
    </div>
  );
}

/** A quiet skeleton while a call is in flight; it fades in only if the wait is noticeable. */
export function Loading({ label, rows = 3, panel }: { label: string; rows?: number; panel?: boolean }) {
  return (
    <div className={`loading ${panel ? "in-panel" : ""}`} role="status" aria-live="polite">
      <span className="sr-only">{label}</span>
      <span className="skeleton title" aria-hidden="true" />
      {Array.from({ length: rows }, (_, i) => (
        <span key={i} className="skeleton row" style={{ width: `${100 - ((i * 13) % 30)}%` }} aria-hidden="true" />
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

const DRAWER_OUT_MS = 180;
const reducedMotion = () => typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/** A panel that slides in from the right over a fading scrim; closing slides it out, then goes back to `back`
 *  (the hash without the drawer). Escape, the scrim and the close button all close it; focus moves into it. */
export function Drawer({ title, back, children, wide }: { title: ReactNode; back: string; children: ReactNode; wide?: boolean }) {
  const [closing, setClosing] = useState(false);
  const panel = useRef<HTMLElement>(null);
  const titleId = useId();
  const timer = useRef<number | undefined>(undefined);
  // Whatever had focus when the drawer opened (read on the first render, before focus moves in).
  const opener = useRef<HTMLElement | null>(typeof document === "undefined" ? null : (document.activeElement as HTMLElement | null));

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
    }, DRAWER_OUT_MS);
  }, [back]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [close]);

  // Focus goes into the drawer, the page behind stops scrolling, and both come back when it closes.
  useEffect(() => {
    const before = opener.current;
    if (!panel.current?.contains(document.activeElement)) panel.current?.focus({ preventScroll: true });
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = overflow;
      window.clearTimeout(timer.current);
      timer.current = undefined;
      if (before && document.contains(before)) before.focus({ preventScroll: true });
    };
  }, []);

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
            <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
              <path d="M3 3l8 8M11 3l-8 8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
            </svg>
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
