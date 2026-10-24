// Formatting and the one rule every screen follows: object ids and dollar figures link into the trace.

import { Fragment, type ReactNode } from "react";

export const ID = /\b(?:JE|CL|D|A|P|PR|R|M|AS|EV)-\d[\d-]*\b/g;
const TOKEN = /\b(?:JE|CL|D|A|P|PR|R|M|AS|EV)-\d[\d-]*\b|-?\$\d[\d,]*(?:\.\d{2})?/g;

export function isTraceable(id: string): boolean {
  return new RegExp(`^${ID.source}$`).test(id) || /^(deal|account)\/[\w-]+$/.test(id);
}

export function usd(cents: number | null | undefined): string {
  if (cents === null || cents === undefined) return "";
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(cents);
  return `${sign}$${Math.floor(abs / 100).toLocaleString("en-US")}.${String(abs % 100).padStart(2, "0")}`;
}

export function when(ts: string | null | undefined): string {
  if (!ts) return "";
  return ts.replace("T", " ").replace(/:00Z$/, "Z").replace(/Z$/, "");
}

export function traceHref(id: string): string {
  return `#trace/${encodeURIComponent(id)}`;
}

export function TraceLink({ id, children }: { id: string; children?: ReactNode }) {
  return (
    <a className="ref" href={traceHref(id)} title={`Trace ${id}`}>
      {children ?? id}
    </a>
  );
}

export function Money({ cents, trace }: { cents: number | null | undefined; trace?: string | null }) {
  if (cents === null || cents === undefined) return null;
  const text = usd(cents);
  return trace && isTraceable(trace) ? (
    <a className="money" href={traceHref(trace)} title={`Trace ${trace}`}>
      {text}
    </a>
  ) : (
    <span className="money plain">{text}</span>
  );
}

/** Text with every id linked, and every $ figure linked to `trace` (or the first id in the text). */
export function Rich({ text, trace }: { text: string; trace?: string | null }) {
  const first = trace ?? text.match(ID)?.[0] ?? null;
  const parts: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(TOKEN)) {
    const token = match[0];
    const at = match.index ?? 0;
    parts.push(text.slice(last, at));
    if (token.includes("$")) {
      parts.push(
        first ? (
          <a key={at} className="money" href={traceHref(first)} title={`Trace ${first}`}>
            {token}
          </a>
        ) : (
          <span key={at} className="money plain">{token}</span>
        ),
      );
    } else {
      parts.push(<TraceLink key={at} id={token} />);
    }
    last = at + token.length;
  }
  parts.push(text.slice(last));
  return (
    <>
      {parts.map((part, i) => (
        <Fragment key={i}>{part}</Fragment>
      ))}
    </>
  );
}

export function personName(id: string, people: { id: string; name: string }[]): string {
  if (id.startsWith("seat:")) return id === "seat:commissions_analyst" ? "Quota · analyst" : "Quota · accountant";
  return people.find((p) => p.id === id)?.name ?? id;
}
