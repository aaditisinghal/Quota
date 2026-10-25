import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  api,
  type ContractCard,
  type Pending,
  type Principal,
  type Recipient,
  type TimelineItem,
  type Trace,
  type TraceNode,
} from "./api";
import { ChatPane } from "./ChatPane";
import { FlashMoney } from "./Flash";
import { Money, TraceLink, When, accountName, learnAccounts, personName, pt, usd } from "./format";

// The first twenty seconds of the demo: one disputed line, the two records that disagree, the rule that decides,
// and who said what about it, in order. Everything here is read from the trace and the timeline.

const FIELD_NAMES: Record<string, string> = {
  crm_close_date: "Close date",
  countersigned_at: "Countersigned",
  contract_date: "Contract date",
  crm_amount_cents: "Amount",
  acv_cents: "ACV",
  term_months: "Term",
  stage: "Stage",
  owner_id: "Owner",
  account: "Account",
};
const DISPUTED: Record<string, string> = { booking_date: "booking date", base: "commissionable base" };

type Props = {
  line: string;
  items: TimelineItem[];
  people: Principal[];
  pending: Pending[];
  principal: string;
  setPrincipal: (id: string) => void;
  away: Record<string, string>;
  version: number;
  onChange: () => void;
  onOpen: (line: string) => void; // the case picker chose another line
  onRep?: (rep: string) => void; // the open line's rep, once its trace has loaded
  follow?: CaseFollow | null; // why the speaker picker opened this case, or that nothing waits on that person
};

/** What following a person found: their case line (null when nothing waits on them) and why it was chosen. */
export type CaseFollow = { person: string; line: string | null; note: string };

const SELECT_STYLE = { border: "1px solid var(--line)", borderRadius: 6, padding: "4px 8px", background: "var(--panel)", font: "inherit", color: "inherit", maxWidth: "100%" };

export function CaseView({ line, items, people, pending, principal, setPrincipal, away, version, onChange, onOpen, onRep, follow }: Props) {
  // The trace is kept with the line it answers, so a newly chosen case never shows under the previous one's title.
  const [loaded, setLoaded] = useState<{ line: string; trace: Trace | null; error: string | null } | null>(null);

  useEffect(() => {
    let live = true;
    api
      .trace(line, null)
      .then((t) => {
        if (!live) return;
        learnAccounts(t.nodes);
        setLoaded({ line, trace: t, error: null });
      })
      .catch((e: Error) => live && setLoaded({ line, trace: null, error: e.message }));
    return () => {
      live = false;
    };
  }, [line, version]);

  const current = loaded?.line === line ? loaded : null;
  const trace = current?.trace ?? null;
  const error = current?.error ?? null;
  const facts = useMemo(() => (trace ? caseFacts(trace, items) : null), [trace, items]);
  const steps = useMemo(() => (trace ? chain(trace, items, people, pending) : []), [trace, items, people, pending]);
  // The title comes from the line itself, so it names the rep and customer even when there is nothing to compare.
  const root = trace?.nodes.find((n) => n.id === trace.root && n.kind === "line");
  const lineRep = root ? String(root.rep ?? "") : "";
  useEffect(() => {
    if (lineRep && onRep) onRep(lineRep);
  }, [lineRep, onRep]);

  // The case picker: the selected rep's contract lines, or every rep's lines for anyone else.
  const rep = isRep(principal, people);
  const known = people.length > 0;
  const [options, setOptions] = useState<CaseOption[]>([]);
  useEffect(() => {
    if (!known) return;
    let live = true;
    caseOptions(principal, people)
      .then((o) => live && setOptions(o))
      .catch(() => live && setOptions([]));
    return () => {
      live = false;
    };
    // The list follows the speaker, not every state poll (people is a new array on each one).
  }, [principal, rep, known]);

  const title = root ? `${accountName(String(root.deal ?? ""))} · ${personName(lineRep, people)}'s commission` : null;
  const unknown = !!error || (!!trace && !facts);
  // The note only speaks for the person still selected, and only about the case it chose.
  const note =
    follow && follow.person === principal
      ? follow.line === null
        ? `Nothing is waiting on ${personName(follow.person, people)}; showing ${title ?? line}`
        : follow.line === line
          ? follow.note
          : null
      : null;

  return (
    <section className="case">
      <div className="case-head">
        <span className="kind-tag">case</span>
        {title ? <h2>{title}</h2> : <h2 className="muted">{unknown ? `No case on ${line}` : `Loading ${line}…`}</h2>}
        <TraceLink id={line} />
      </div>
      <div className="case-head">
        <label className="muted" htmlFor="case-pick">
          Open a case
        </label>
        <select id="case-pick" style={SELECT_STYLE} value={line} onChange={(e) => e.target.value !== line && onOpen(e.target.value)}>
          {!options.some((o) => o.line === line) && <option value={line}>{title ? `${title} · ${line}` : line}</option>}
          {options.map((o) => (
            <option key={o.line} value={o.line}>
              {o.label}
            </option>
          ))}
        </select>
        {note && <span className="muted">{note}</span>}
      </div>
      <div className="case-grid">
        {facts ? (
          <>
            <Evidence facts={facts} />
            <Decision facts={facts} steps={steps} />
          </>
        ) : (
          <>
            <div className="case-col">
              {unknown ? (
                <div className="card unknown">
                  <h3>No case on {line}</h3>
                  <p className="muted">{error ?? "This line has no source disagreement to show."}</p>
                </div>
              ) : (
                <p className="muted">Loading {line}…</p>
              )}
            </div>
            <div className="case-col" />
          </>
        )}
        <div className="case-col case-chat">
          <h3>Conversation</h3>
          <ChatPane principal={principal} setPrincipal={setPrincipal} people={people} pending={pending} away={away}
                    version={version} onChange={onChange} compact />
        </div>
      </div>
    </section>
  );
}

function isRep(id: string, people: Principal[]): boolean {
  return people.find((p) => p.id === id)?.capabilities.includes("dispute:own_lines") ?? false;
}

type CaseLine = {
  line: string;
  rep: string;
  customer: string;
  status: string;
  amount: number | null;
  at: string;
  confidence: number | null;
  amendment: boolean;
};

const GONE = new Set(["superseded", "clawed_back", "voided", "void"]);
/** A line that still pays: not replaced, recovered or voided, and not itself a negative correction. */
const live = (l: { status: string; amount_cents?: number | null; amount?: number | null }) =>
  !GONE.has(l.status) && (l.amount_cents ?? l.amount ?? 0) >= 0;

/** Every commission line on these contracts and their amendments, with the server's confidence for its recipient. */
function contractLines(contracts: ContractCard[]): CaseLine[] {
  const out: CaseLine[] = [];
  const seen = new Set<string>();
  const add = (l: CaseLine) => {
    if (seen.has(l.line)) return;
    seen.add(l.line);
    out.push(l);
  };
  for (const c of contracts) {
    // The confidence the server gave this line's recipient (recipients[] where who is the line's rep).
    const score = (line: string, rep: string, recipients: Recipient[] | undefined) =>
      recipients?.find((r) => r.line === line && (!rep || r.who === rep))?.confidence?.score ?? null;
    for (const l of c.lines) {
      add({ line: l.id, rep: l.rep, customer: c.customer, status: l.status, amount: l.amount_cents, at: c.countersigned_at ?? "",
            confidence: score(l.id, l.rep, c.recipients), amendment: false });
    }
    for (const child of c.children ?? []) {
      for (const inc of child.incremental_lines ?? []) {
        const id = typeof inc === "string" ? inc : inc.id;
        const who = child.recipients?.find((r) => r.line === id)?.who ?? "";
        add({ line: id, rep: who, customer: c.customer, status: "", amount: typeof inc === "string" ? null : inc.amount_cents,
              at: child.signed_at ?? "", confidence: score(id, who, child.recipients), amendment: true });
      }
    }
  }
  return out;
}

type CaseOption = { line: string; label: string };

/** The case picker's choices, "Customer · rep · line id": a rep's own lines, or every rep's lines for anyone else. */
async function caseOptions(person: string, people: Principal[]): Promise<CaseOption[]> {
  const rep = isRep(person, people);
  const details = rep
    ? [await api.rep(person)]
    : await Promise.all((await api.reps()).map((r) => api.rep(r.rep).catch(() => null)));
  const lines = contractLines(details.flatMap((d) => d?.contracts ?? [])).filter((l) => !rep || l.rep === person);
  return lines.map((l) => ({
    line: l.line,
    label: `${l.customer}${l.amendment ? " amendment" : ""} · ${l.rep ? personName(l.rep, people) : "?"} · ${l.line}${
      GONE.has(l.status) ? ` (${l.status.replace(/_/g, " ")})` : ""
    }`,
  }));
}

/**
 * The line a person's case opens on (null when nothing is waiting on them) and a short note saying why. A rep's is
 * their least certain line (the lowest confidence score, the most recent on a tie; else their first line). Anyone
 * else's is the line behind the first item in their Needs-you group. Only which line to show is chosen here; every
 * figure stays the server's.
 */
export async function caseLineFor(person: string, people: Principal[]): Promise<{ line: string | null; note: string }> {
  const name = personName(person, people);
  if (isRep(person, people)) {
    const own = contractLines((await api.rep(person)).contracts).filter((l) => l.rep === person);
    const paying = own.filter(live);
    const scored = paying.filter((l) => l.confidence !== null);
    scored.sort((a, b) => (a.confidence ?? 0) - (b.confidence ?? 0) || b.at.localeCompare(a.at) || b.line.localeCompare(a.line));
    if (scored[0]) return { line: scored[0].line, note: `${name}'s least certain line · confidence ${scored[0].confidence}` };
    const first = paying[0] ?? own[0];
    return first ? { line: first.line, note: `${name}'s first line` } : { line: null, note: "" };
  }
  // An approver opens the least certain of the items waiting on them (Priya: Summit at 40 before Acme at 80),
  // so switching to them always shows what most needs their judgement; ties go to the newest item.
  const group = (await api.needsYou()).people.find((g) => g.person === person);
  const candidates: { line: string; score: number; order: number; note: string }[] = [];
  for (const [order, item] of (group?.items ?? []).entries()) {
    for (const deal of item.deals ?? []) {
      const card = await api.contract(deal).catch(() => null); // a deal with no contract card yet: try the next one
      const line = card?.lines.find(live) ?? card?.lines[0];
      if (!line) continue;
      const score = card?.recipients.find((r) => r.line === line.id)?.confidence?.score ?? 100;
      const what = item.kind.replace(/_/g, " ");
      candidates.push({ line: line.id, score, order,
                        note: `Waiting on ${name}: ${item.id} ${what}${item.impact_cents ? ` · ${usd(item.impact_cents)}` : ""} · confidence ${score}` });
      break;
    }
  }
  candidates.sort((a, b) => a.score - b.score || b.order - a.order);
  return candidates[0] ? { line: candidates[0].line, note: candidates[0].note } : { line: null, note: "" };
}

/** One source record shown as evidence: the one used, the one it outranked, or one read alongside them. */
type SourceRecord = { node: TraceNode; field: string; role: "used" | "outranked" | "read"; note: string };

type Facts = {
  deal: string;
  rep: string;
  field: string | null; // what the deciding rule resolved, or null when no rule had to choose
  original: TraceNode;
  current: TraceNode;
  records: SourceRecord[];
  rule: string;
  ruleText: string | null;
  claimed: number | null;
  disputed: boolean;
  conceded: boolean;
};

type Disagreement = { source: string; basis: string; effect_cents: number | null };

// The field each source holds for a resolved question, for a record the trace did not highlight.
const COUNTERPART: Record<string, Record<string, string>> = {
  booking_date: { deals: "crm_close_date", contracts: "countersigned_at" },
  base: { deals: "crm_amount_cents", contracts: "acv_cents" },
  splits: { deals: "owner_id", contracts: "acv_cents" },
};

function counterpartField(node: TraceNode, field: string | null): string {
  const table = String(node.table ?? "");
  return COUNTERPART[field ?? ""]?.[table] ?? COUNTERPART.base[table] ?? "";
}

/** The newest version of this deal's record in one source table, among the nodes the trace read. */
function latestRecord(trace: Trace, table: string, deal: string): TraceNode | undefined {
  const version = (n: TraceNode) => Number(String(n.id).match(/:v(\d+)$/)?.[1] ?? 0);
  return trace.nodes
    .filter((n) => n.table === table && String(n.id).startsWith(`${table}:${deal}:`))
    .sort((a, b) => version(b) - version(a))[0];
}

function caseFacts(trace: Trace, items: TimelineItem[]): Facts | null {
  const nodes = new Map(trace.nodes.map((n) => [n.id, n]));
  const original = trace.root ? nodes.get(trace.root) : undefined;
  if (!original || original.kind !== "line") return null;
  // Follow "corrected by" to the line that pays now. A negative correction (a clawback, a quarter true-up) is a
  // recovery of that line, not a new amount for the case, so the walk stops before it.
  let current = original;
  for (let hop = 0; hop < 10; hop++) {
    const next = trace.edges.find((e) => e.from === current.id && e.label === "corrected by");
    const line = next ? nodes.get(next.to) : undefined;
    if (!line || (line.amount_cents ?? 0) < 0) break;
    current = line;
  }
  const disputes = items.filter((i) => i.kind === "DECISION" && i.text.includes(` dispute ${original.id}:`));
  const asserted = disputes.map((i) => i.text.match(/\((\w+) asserted /)?.[1]).find(Boolean);
  const resolutions = trace.nodes.filter((n) => n.kind === "resolution");
  const alternativesOf = (n: TraceNode) => (n.disagreements as Disagreement[] | undefined) ?? [];
  const chosenOf = (n: TraceNode) => trace.edges.find((e) => e.from === n.id && e.label === "chosen")?.to;
  const effect = (n: TraceNode) => Math.max(0, ...alternativesOf(n).map((d) => Math.abs(d.effect_cents ?? 0)));
  // The disputed field; else the one where the sources disagree by the most money; else one where another record
  // disagrees at all (a line nobody disputed still shows CRM against contract); else the first the rule resolved.
  const resolution =
    resolutions.find((n) => n.field === asserted) ??
    resolutions.filter((n) => effect(n) > 0).sort((a, b) => effect(b) - effect(a))[0] ??
    resolutions.find((n) => alternativesOf(n).some((d) => d.source !== chosenOf(n))) ??
    resolutions[0];
  const field = resolution ? String(resolution.field) : null;
  const chosenId = resolution ? chosenOf(resolution) : undefined;
  // The alternative that would move the money most; on a tie, a different record from the one used.
  const biggest = (resolution ? alternativesOf(resolution) : [])
    .map((d, i) => ({ d, i }))
    .sort(
      (a, b) =>
        Math.abs(b.d.effect_cents ?? 0) - Math.abs(a.d.effect_cents ?? 0) ||
        Number(a.d.source === chosenId) - Number(b.d.source === chosenId) ||
        a.i - b.i,
    )[0]?.d;
  const chosenNode = chosenId ? nodes.get(chosenId) : undefined;
  const altNode = biggest ? nodes.get(biggest.source) : undefined;
  const rule = String(resolution?.rule ?? "");
  const deal = String(original.deal ?? "");

  const records: SourceRecord[] = [];
  if (altNode && biggest) {
    const e = biggest.effect_cents ?? 0;
    records.push({
      node: altNode,
      field: highlightField(altNode, field ?? "", "alternative") || counterpartField(altNode, field),
      role: "outranked",
      note: e ? `Would pay ${usd(Math.abs(e))} ${e > 0 ? "more" : "less"} if it counted` : "Disagrees, but would pay the same if it counted",
    });
  }
  if (chosenNode) {
    records.push({
      node: chosenNode,
      field: highlightField(chosenNode, field ?? "", "chosen") || counterpartField(chosenNode, field),
      role: "used",
      note: "Used for the commission",
    });
  }
  // With no disagreement to show, the CRM and contract records still sit side by side (CRM first).
  if (!altNode) {
    for (const table of ["deals", "contracts"]) {
      if (records.some((r) => r.node.table === table)) continue;
      const node = latestRecord(trace, table, deal);
      if (!node) continue;
      const read: SourceRecord = {
        node,
        field: counterpartField(node, field),
        role: "read",
        note: chosenNode ? "Read for this calculation; it did not outrank the record used" : "Read for this calculation",
      };
      if (table === "deals") records.unshift(read);
      else records.push(read);
    }
  }
  // The analyst's reply states the rule in words; use the first one that names it.
  const ruleText =
    items
      .filter((i) => i.kind === "REPLY")
      .flatMap((i) => i.text.split("\n"))
      .find((l) => l.startsWith(`Rule: ${rule}:`))
      ?.slice(`Rule: ${rule}: `.length) ?? null;
  return {
    deal,
    rep: String(original.rep ?? ""),
    field,
    original,
    current,
    records,
    rule,
    ruleText,
    // Only a record that would pay more is what a rep claims; one that would pay less was simply outranked.
    claimed:
      biggest && (biggest.effect_cents ?? 0) > 0 && typeof original.amount_cents === "number"
        ? original.amount_cents + (biggest.effect_cents ?? 0)
        : null,
    disputed: disputes.length > 0,
    conceded: disputes.some((d) => / dispute CL-[\d-]+: conceded/.test(d.text)),
  };
}

/** Where an intake-loaded record came from (R6): its export file, row, and the raw CSV line. */
export function provenance(node: TraceNode): { file: string; row: number; raw?: string } | null {
  const source = node.source as { file?: string; row?: number; raw?: string } | null | undefined;
  return source && typeof source === "object" && source.file ? { file: source.file, row: source.row ?? 0, raw: source.raw } : null;
}

function highlightField(node: TraceNode, resolves: string, role: string): string {
  const marks = (node.highlight as { field: string; role: string; resolves: string }[]) ?? [];
  return marks.find((m) => m.resolves === resolves && m.role === role)?.field ?? "";
}

function display(field: string, value: unknown): string {
  if (value === null || value === undefined) return "";
  if (field.endsWith("_cents") && typeof value === "number") return usd(value);
  if (field === "term_months") return `${value} months`;
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}/.test(value)) return pt(value, true);
  return typeof value === "string" ? value : JSON.stringify(value);
}

function Evidence({ facts }: { facts: Facts }) {
  const used = facts.records.find((r) => r.role === "used");
  const field = facts.field ?? "";
  return (
    <div className="case-col">
      <h3>Evidence</h3>
      {facts.rule ? (
        <div className="rule-banner">
          <span className="muted">The rule that decides</span>
          <strong>
            {facts.rule}
            {facts.ruleText ? `: ${facts.ruleText}` : `: ${DISPUTED[field] ?? field} comes from the ${used ? sourceKind(used.node) : "first source"}`}
          </strong>
        </div>
      ) : (
        <p className="muted">No rule had to choose between sources on this line.</p>
      )}
      <div className="evidence">
        {facts.records.map((r) => (
          <SourceCard key={`${r.role}:${r.node.id}`} node={r.node} field={r.field} role={r.role} note={r.note} />
        ))}
        {facts.records.length === 0 && <p className="muted">The trace holds no CRM or contract record for this line.</p>}
      </div>
    </div>
  );
}

function sourceKind(node: TraceNode): string {
  const table = String(node.table ?? "");
  return table === "deals" ? "CRM" : table === "contracts" ? "signed contract" : table.replace(/_/g, " ");
}

function SourceCard({ node, field, role, note }: { node: TraceNode; field: string; role: SourceRecord["role"]; note: string }) {
  const snippet = (node.snippet as Record<string, unknown>) ?? {};
  const table = String(node.table ?? "");
  const title = table === "deals" ? "CRM opportunity" : table === "contracts" ? "E-signature · signed contract" : table;
  const system = String(snippet.source ?? "");
  const origin = provenance(node);
  const file = origin ? `${origin.file} · row ${origin.row}` : null;
  const shown = ["account", "crm_amount_cents", "acv_cents", "term_months", "owner_id"].filter((k) => k in snippet && k !== field);
  return (
    <div className={`card source ${role}`}>
      <div className="card-head">
        <strong>{title}</strong>
        <span className={`tag ${role === "used" ? "tag-used" : ""}`}>{role}</span>
        {system && <span className="muted">from {system}</span>}
      </div>
      {field && (
        <div className="source-key">
          <span className="muted">{FIELD_NAMES[field] ?? field}</span>
          <span className="source-value">{display(field, snippet[field])}</span>
        </div>
      )}
      <dl>
        {shown.map((k) => (
          <div key={k} className="field">
            <dt>{FIELD_NAMES[k] ?? k}</dt>
            <dd>{display(k, snippet[k])}</dd>
          </div>
        ))}
      </dl>
      <p className="muted source-note">{note}</p>
      <details className="details">
        <summary>{file ?? `${table} · ${String(snippet.id ?? node.id)}`} · raw row</summary>
        {origin?.raw ? <pre className="raw">{origin.raw}</pre> : null}
        <table className="snippet">
          <tbody>
            {Object.entries(snippet).map(([k, v]) => (
              <tr key={k} className={k === field ? "hl-chosen" : ""}>
                <th>{k}</th>
                <td>
                  <span className="val">{typeof v === "string" ? v : JSON.stringify(v)}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="muted mono">
          {node.id} · known <When ts={String(node.known_at ?? "")} /> · sha256 {String(node.sha256 ?? "").slice(0, 12)}…
        </p>
      </details>
    </div>
  );
}

type Step = {
  at: string;
  label: ReactNode;
  detail?: string;
  ids: string[];
  codes?: string[];
  tone?: "refused" | "human" | "done";
  held?: number; // dispute holds folded into this step
};

function Decision({ facts, steps }: { facts: Facts; steps: Step[] }) {
  const changed = facts.current.id !== facts.original.id;
  const waiting = steps.some((s) => s.tone === "human");
  return (
    <div className="case-col">
      <h3>Decision and approval chain</h3>
      <div className="figure">
        <div className="figure-main">
          <FlashMoney id={`case:${facts.original.id}`} cents={facts.current.amount_cents ?? 0} trace={facts.current.id} />
        </div>
        <div className="figure-sub">
          {changed ? (
            <>
              paid as claimed, by exception · was <Money cents={facts.original.amount_cents} trace={facts.original.id} /> under the plan
            </>
          ) : facts.conceded ? (
            <>as claimed · the dispute was conceded on the evidence</>
          ) : facts.claimed !== null ? (
            <>
              vs <span className="claimed">{usd(facts.claimed)}</span> {facts.disputed ? "claimed" : "if the outranked record counted"}
              {facts.disputed && <> · difference {usd(facts.claimed - (facts.original.amount_cents ?? 0))}</>}
            </>
          ) : null}
        </div>
        <div className="figure-status">
          <span className="muted">
            {String(facts.current.quarter)} · line {facts.current.id} · {String(facts.current.status).replace(/_/g, " ")}
          </span>
          {waiting && <span className="badge accent">waiting on a person</span>}
        </div>
      </div>
      <ol className="chain">
        {steps.map((s, i) => (
          <li key={i} className={`step ${s.tone ?? ""}`}>
            <span className="step-dot" aria-hidden>
              {s.tone === "refused" ? "🔒" : ""}
            </span>
            <div>
              <div className="step-label">{s.label}</div>
              {s.detail && <div className="muted">{s.detail}</div>}
              <div className="step-meta">
                <When ts={s.at} />
                {s.ids.map((id) => (
                  <TraceLink key={id} id={id} />
                ))}
                {s.codes?.map((c) => (
                  <code key={c} className="code">
                    {c}
                  </code>
                ))}
              </div>
            </div>
          </li>
        ))}
        {steps.length === 0 && <li className="muted">Nothing has happened on this line yet.</li>}
      </ol>
    </div>
  );
}

const KEEP_DECISIONS = /^D-\d+ (commission_calc|dispute|escalation|resubmission_check|integrity_check|authority_check|commitment_check)\b/;

/** The timeline items about this line, in order, as plain-English steps. */
function chain(trace: Trace, items: TimelineItem[], people: Principal[], pending: Pending[]): Step[] {
  const root = trace.root ?? "";
  const deal = String(trace.nodes.find((n) => n.id === root)?.deal ?? "");
  const ids = new Set([root, ...trace.edges.filter((e) => e.from === root && e.label === "calculated in").map((e) => e.to)]);
  const name = (id: string) => personName(id, people);
  const rep = name(String(trace.nodes.find((n) => n.id === root)?.rep ?? ""));
  const steps: Step[] = [];
  const dealWord = new RegExp(`\\b${deal}\\b`);

  for (const item of items) {
    if (!["DECISION", "APPROVAL", "REFUSED", "ENTRY"].includes(item.kind)) continue;
    // A grant about this deal joins the case by name: approvals of it, and the authority check on who wrote it.
    const aboutDeal = dealWord.test(item.text) || item.text.includes(accountName(deal));
    const hit =
      item.refs.some((r) => ids.has(r)) ||
      (item.kind === "APPROVAL" && / plan_exception /.test(item.text) && aboutDeal) ||
      (item.kind === "DECISION" && / (authority|commitment)_check /.test(item.text) && aboutDeal);
    if (!hit) continue;
    if (item.kind === "DECISION" && !KEEP_DECISIONS.test(item.text)) continue;
    item.refs.filter((r) => /^(A|CL|D|P)-/.test(r)).forEach((r) => ids.add(r));
    const head = item.text.match(/^([A-Z]+-[\d-]+)/)?.[1] ?? "";
    const t = item.text;
    let step: Step | null = null;

    if (item.kind === "DECISION") {
      const calc = t.match(/commission_calc \S+: (\d{4}Q\d) (\w+) tier (\$[\d,.]+)/);
      const dispute = t.match(/ dispute (CL-\d+): (\w+)/);
      if (calc) {
        step = { at: item.clock, ids: [head], label: <>Quota calculated {calc[3]}</>, detail: `${calc[1]}, ${calc[2]} tier` };
      } else if (dispute) {
        // Repeated holds collapse into one step: "held twice".
        const last = steps[steps.length - 1];
        if (dispute[2] === "held" && last?.held) {
          last.held += 1;
          last.ids.push(head);
          last.label = <>{rep} disputed · held {times(last.held)}</>;
          if (/no_new_evidence/.test(t)) last.detail = `${last.detail ?? ""} · nothing new the second time`;
          continue;
        }
        const asserted = t.match(/\((\w+) asserted ([^;)]+)/);
        step = {
          at: item.clock,
          ids: [head],
          label: <>{rep} disputed · {dispute[2]}</>,
          detail: asserted ? `asked for ${DISPUTED[asserted[1]] ?? asserted[1]} ${pt(asserted[2].trim())}` : undefined,
          tone: dispute[2] === "held" ? undefined : "done",
          held: dispute[2] === "held" ? 1 : undefined,
        };
      } else if (/ authority_check /.test(t)) {
        const grant = t.match(/authority_check \S+: \w+ from ([\w.]+) routed to ([\w.]+): (.*)$/);
        const conflict = grant?.[3].match(/conflict of interest: (.*)$/)?.[1];
        step = {
          at: item.clock,
          ids: [head],
          label: grant ? <>{name(grant[1])} tried to grant it · routed to {name(grant[2])}</> : <>Authority check</>,
          detail: conflict ? `conflict of interest: ${conflict}` : grant?.[3].split(";")[0],
          tone: "refused",
        };
      } else if (/ commitment_check /.test(t)) {
        // A term promised outside the signed contract holds the commission until someone reviews it.
        const found = t.match(/commitment_check \S+: (\w+): (.*)$/);
        const held = t.match(/(\$[\d,.]+) held/)?.[1];
        const what = found ? found[1].toLowerCase().replace(/_/g, " ") : "commitment check";
        step = {
          at: item.clock,
          ids: [head],
          label: <>{held ? `Quota held ${held}` : "Quota checked"} · {what}</>,
          detail: found ? found[2].split(";")[0].slice(0, 180) : undefined,
          codes: found ? [found[1]] : undefined,
        };
      } else if (/ escalation /.test(t)) {
        const to = t.match(/-> ([\w.]+) \(([^)]+)\)/);
        step = { at: item.clock, ids: [head], label: <>Escalated to {to ? `${name(to[1])} (${to[2]})` : "the plan owner"}</> };
      } else {
        step = { at: item.clock, ids: [head], label: t.replace(/^D-\d+ /, "") };
      }
    } else if (item.kind === "APPROVAL") {
      const ask = t.match(/^(A-\d+) plan_exception .*?-> ([\w.]+): delta (\$[\d,.]+) \((\$[\d,.]+) -> (\$[\d,.]+)\)/);
      const decided = t.match(/^(A-\d+) \S+ \S+ (approved|rejected) -> (\w+)(?: \(limit (\$[\d,.]+) < (\$[\d,.]+)\))?(?:; (A-\d+) -> ([\w.]+))?/);
      const precedent = t.match(/^(P-\d+) precedent \S+ (\S+)=(\S+) \((\d{4}Q\d)\), expires (\S+)/);
      if (ask) {
        step = { at: item.clock, ids: [ask[1]], label: <>Exception asked of {name(ask[2])}</>, detail: `${ask[4]} → ${ask[5]} (${ask[3]} more)` };
      } else if (decided) {
        const who = name(item.actor);
        if (decided[2] === "rejected") {
          step = { at: item.clock, ids: [decided[1]], label: <>{who} rejected</>, tone: "done" };
        } else if (decided[3] === "over_limit_routed") {
          steps.push({ at: item.clock, ids: [decided[1]], label: <>{who} approved</> });
          step = {
            at: item.clock,
            ids: decided[6] ? [decided[6]] : [],
            label: <>Over the {decided[4]} limit · routed to {decided[7] ? name(decided[7]) : "the backup"}</>,
            detail: `${decided[5]} needs a higher approver`,
          };
        } else {
          step = { at: item.clock, ids: [decided[1]], label: <>{who} approved · {decided[3]}</>, tone: "done" };
        }
      } else if (precedent) {
        step = {
          at: item.clock,
          ids: [precedent[1]],
          label: <>Precedent written</>,
          detail: `${DISPUTED[precedent[2]] ?? precedent[2]} ${pt(precedent[3])} counts in ${precedent[4]} until ${pt(precedent[5])}`,
        };
      } else {
        step = { at: item.clock, ids: [head], label: t.replace(/^[A-Z]+-\d+ /, "") };
      }
    } else if (item.kind === "REFUSED") {
      const what = t.match(/^R-\d+ (\w+) (\S+):/);
      const act = what ? `${what[1].replace(/_/g, " ").replace("decide approval", "decide")} ${what[2]}` : "act";
      // A seat refused at a person's request names that person: "Jordan Kim asked Quota to decide A-0002 itself".
      const asker = item.actor.startsWith("seat:")
        ? items.find((m) => m.kind === "MESSAGE" && m.clock === item.clock && !m.actor.startsWith("seat:"))?.actor
        : undefined;
      step = {
        at: item.clock,
        ids: [head],
        label: asker ? <>{name(asker)} asked Quota to {act} itself · refused</> : <>{name(item.actor)} tried to {act} · refused</>,
        codes: item.codes,
        tone: "refused",
      };
    } else if (item.kind === "ENTRY") {
      const superseded = t.match(/^(CL-\d+) -> superseded by (CL-\d+)/);
      if (superseded) step = { at: item.clock, ids: [superseded[2]], label: <>{superseded[1]} replaced by {superseded[2]}</>, tone: "done" };
    }
    if (step) steps.push(step);
  }

  // An approval waits on this line when it was named in the chain, or is about a decision in it (a review of a hold).
  for (const a of pending.filter((p) => ids.has(p.id) || ids.has(p.subject))) {
    steps.push({ at: a.since, ids: [a.id], label: <>Waiting on {name(a.approver)}</>, detail: `${usd(a.amount_cents)} to decide`, tone: "human" });
  }
  return steps;
}

function times(n: number): string {
  return n === 2 ? "twice" : `${n} times`;
}
