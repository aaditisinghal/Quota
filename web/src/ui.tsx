import { useEffect, useState, type ReactNode } from "react";
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
    <div className="card unknown">
      <h3>{what} is not available on this server yet</h3>
      <p className="muted">
        The screen reads <code>GET {path}</code>, which this server does not answer.
      </p>
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

/** A panel that slides in from the right; closing goes back to `back` (the hash without the drawer). */
export function Drawer({ title, back, children, wide }: { title: ReactNode; back: string; children: ReactNode; wide?: boolean }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") window.location.hash = back;
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [back]);
  return (
    <div className="drawer-wrap" onMouseDown={(e) => e.target === e.currentTarget && (window.location.hash = back)}>
      <aside className={`drawer ${wide ? "wide" : ""}`} role="dialog" aria-modal="true">
        <div className="drawer-head">
          <h2>{title}</h2>
          <a className="drawer-close" href={`#${back}`} aria-label="Close">
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
