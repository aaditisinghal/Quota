// Formatting and the one rule every screen follows: object ids and dollar figures link into the trace.
// Everything a person reads is plain English: Pacific times, accounting negatives, things by name. The raw
// form (ISO time, source ref, the line as the API wrote it) stays one hover or one "Details" click away.

import { Fragment, type ReactNode } from "react";

export const ID = /\b(?:JE|CL|D|A|P|PR|R|M|AS|EV)-\d[\d-]*\b/g;
const SOURCE_REF = /\b(?:deals|contracts|esign_events|plans|commission_lines|messages|decisions|billing_events|people):[\w.-]+(?::v\d+)?/;
const TOKEN = new RegExp(
  [
    ID.source, // an object id: links into the trace
    /-?\$\d[\d,]*(?:\.\d{2})?/.source, // a dollar figure: links to the line's object
    SOURCE_REF.source, // a source record: named, the raw ref in its tooltip
    /\b\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}Z)?\b/.source, // a time or date: Pacific, ISO in its tooltip
    /\b[a-z]+(?:_[a-z0-9]+)+\b/.source, // a snake_case word from the engine
  ].join("|"),
  "g",
);
const WORDS: Record<string, string> = {
  crm_close_date: "CRM close date",
  crm_amount_cents: "CRM amount",
  acv_cents: "ACV",
  countersigned_at: "countersigned",
  contract_date: "contract date",
  booking_date: "booking date",
};

export function isTraceable(id: string): boolean {
  return new RegExp(`^${ID.source}$`).test(id) || /^(deal|account)\/[\w-]+$/.test(id);
}

/** Cents as dollars; a negative in parentheses, as an accountant writes it. */
export function usd(cents: number | null | undefined): string {
  if (cents === null || cents === undefined) return "";
  const abs = Math.abs(cents);
  const text = `$${Math.floor(abs / 100).toLocaleString("en-US")}.${String(abs % 100).padStart(2, "0")}`;
  return cents < 0 ? `(${text})` : text;
}

// The company keeps Pacific time; the API speaks UTC. Midnight and 23:59:59 UTC are dates resolved from a plain
// YYYY-MM-DD (a CRM close date), so they show as that date, not as an afternoon the day before.
const PT = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Los_Angeles",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
});
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
let worldYear = "2026";

/** The year the screens leave out (the world clock's); other years are spelled out. */
export function setWorldYear(clock: string | null | undefined) {
  if (clock) worldYear = clock.slice(0, 4);
}

/** `2026-04-02T17:14:00Z` -> `Apr 2, 10:14 AM PT`; `2026-03-31` -> `Mar 31`. */
export function pt(ts: string | null | undefined): string {
  if (!ts) return "";
  const day = ts.match(/^(\d{4})-(\d{2})(?:-(\d{2}))?(?:T(?:00:00:00|23:59:59)Z)?$/);
  if (day) {
    const [, y, m, d] = day;
    const text = d ? `${MONTHS[Number(m) - 1]} ${Number(d)}` : MONTHS[Number(m) - 1];
    return y === worldYear ? text : `${text}, ${y}`;
  }
  const at = new Date(ts);
  if (Number.isNaN(at.getTime())) return ts;
  const text = `${PT.format(at)} PT`;
  return ts.slice(0, 4) === worldYear ? text : text.replace(/^(\w+ \d+)/, `$1, ${ts.slice(0, 4)}`);
}

/** A time in plain English, the raw ISO in its tooltip. */
export function When({ ts, className }: { ts: string | null | undefined; className?: string }) {
  if (!ts) return null;
  return (
    <time className={className} dateTime={ts} title={ts}>
      {pt(ts)}
    </time>
  );
}

// What a source table is, in the words of the person who owns it.
export const SOURCE_NAMES: Record<string, string> = {
  deals: "CRM opportunity",
  contracts: "contract",
  esign_events: "e-signature record",
  plans: "comp plan",
  commission_lines: "commission line",
  messages: "message",
  decisions: "decision",
  billing_events: "billing record",
  people: "HR record",
};

// Account names by deal id, learned from every trace the screens load (a deal record carries its account).
const ACCOUNTS: Record<string, string> = {};

export function learnAccounts(nodes: { kind: string; snippet?: unknown }[]) {
  for (const node of nodes) {
    const snippet = node.snippet as Record<string, unknown> | undefined;
    if (node.kind === "input" && snippet && typeof snippet.account === "string" && typeof snippet.id === "string") {
      ACCOUNTS[snippet.id] = snippet.account;
    }
  }
}

/** Seed facts name accounts too: "deals acme v1 Acme Robotics: …", "birch (Birch Health) $100,000.00". */
export function learnAccountsFromText(text: string) {
  for (const m of text.matchAll(/\bdeals (\w+) v\d+ ([^:]+):|\b([a-z]\w*) \(([A-Z][^)]*)\) \$/g)) {
    const [deal, account] = m[1] ? [m[1], m[2]] : [m[3], m[4]];
    ACCOUNTS[deal] ??= account.trim();
  }
}

export function accountName(deal: string): string {
  return ACCOUNTS[deal] ?? deal.charAt(0).toUpperCase() + deal.slice(1);
}

/** `contracts:acme:v1` -> `Acme Robotics contract`; `deals:acme:v1` -> `Acme Robotics CRM opportunity`. */
export function sourceName(ref: string): string {
  const [table, id] = ref.split(":");
  const what = SOURCE_NAMES[table] ?? table.replace(/_/g, " ");
  if (!id) return what;
  if (table === "deals" || table === "contracts") return `${accountName(id)} ${what}`;
  if (table === "esign_events") return `${what} ${id.replace(/-[a-z_]+$/, "")}`;
  return `${what} ${id}`;
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

/** Text with every id linked, every $ figure linked to `trace` (or the first id in the text), and times,
 * source refs and engine words in plain English. */
export function Rich({ text, trace }: { text: string; trace?: string | null }) {
  const first = trace ?? text.match(ID)?.[0] ?? null;
  const parts: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(TOKEN)) {
    const token = match[0];
    const at = match.index ?? 0;
    parts.push(text.slice(last, at));
    last = at + token.length;
    if (/^-?\$/.test(token)) {
      const shown = token.startsWith("-") ? `(${token.slice(1)})` : token;
      parts.push(
        first ? (
          <a key={at} className="money" href={traceHref(first)} title={`Trace ${first}`}>
            {shown}
          </a>
        ) : (
          <span key={at} className="money plain">{shown}</span>
        ),
      );
    } else if (new RegExp(`^${ID.source}$`).test(token)) {
      parts.push(<TraceLink key={at} id={token} />);
    } else if (SOURCE_REF.test(token) && token.includes(":")) {
      parts.push(
        <span key={at} className="src" title={token}>
          {sourceName(token)}
        </span>,
      );
    } else if (/^\d{4}-/.test(token)) {
      parts.push(<When key={at} ts={token} />);
    } else {
      parts.push(WORDS[token] ?? token.replace(/_/g, " "));
    }
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

const REPLY_LABEL = /^(Claim|Evidence|Rule|Difference|Next step): (.*)$/;
const RAW = new RegExp(`${SOURCE_REF.source}|\\d{4}-\\d{2}-\\d{2}T|[{}]`);

/** A reply or timeline line: labeled rows (Claim / Evidence / Rule / Difference / Next step) when the analyst
 * wrote them, and the text as written under "Details" when the plain-English version changed anything. */
export function Reply({ text, trace }: { text: string; trace?: string | null }) {
  const lines = text.split("\n");
  const labeled = lines.filter((l) => REPLY_LABEL.test(l)).length >= 2;
  return (
    <>
      {labeled ? (
        <dl className="reply">
          {lines.map((l, i) => {
            const m = l.match(REPLY_LABEL);
            return m ? (
              <div key={i} className="field">
                <dt>{m[1]}</dt>
                <dd>
                  <Rich text={m[2]} trace={trace} />
                </dd>
              </div>
            ) : (
              <dd key={i} className="reply-line">
                <Rich text={l} trace={trace} />
              </dd>
            );
          })}
        </dl>
      ) : (
        <Rich text={text} trace={trace} />
      )}
      {RAW.test(text) && (
        <details className="details">
          <summary>Details</summary>
          <pre className="raw">{text}</pre>
        </details>
      )}
    </>
  );
}

export function personName(id: string, people: { id: string; name: string }[]): string {
  if (id.startsWith("seat:")) return id === "seat:commissions_analyst" ? "Quota · analyst" : "Quota · accountant";
  return people.find((p) => p.id === id)?.name ?? id;
}
