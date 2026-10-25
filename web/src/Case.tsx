import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api, type Pending, type Principal, type TimelineItem, type Trace, type TraceNode } from "./api";
import { ChatPane } from "./ChatPane";
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
};

export function CaseView({ line, items, people, pending, principal, setPrincipal, away, version, onChange }: Props) {
  const [trace, setTrace] = useState<Trace | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api
      .trace(line, null)
      .then((t) => {
        if (!live) return;
        learnAccounts(t.nodes);
        setTrace(t);
        setError(null);
      })
      .catch((e: Error) => live && (setTrace(null), setError(e.message)));
    return () => {
      live = false;
    };
  }, [line, version]);

  const facts = useMemo(() => (trace ? caseFacts(trace, items) : null), [trace, items]);
  const steps = useMemo(() => (trace ? chain(trace, items, people, pending) : []), [trace, items, people, pending]);

  if (error || (trace && !facts)) {
    return (
      <section className="case">
        <div className="card unknown">
          <h3>No case on {line}</h3>
          <p className="muted">{error ?? "This line has no source disagreement to show."}</p>
        </div>
      </section>
    );
  }
  if (!trace || !facts) return <section className="case muted">Loading {line}…</section>;

  return (
    <section className="case">
      <div className="case-head">
        <span className="kind-tag">case</span>
        <h2>
          {accountName(facts.deal)} · {personName(facts.rep, people)}'s commission
        </h2>
        <TraceLink id={line} />
      </div>
      <div className="case-grid">
        <Evidence facts={facts} />
        <Decision facts={facts} steps={steps} />
        <div className="case-col case-chat">
          <h3>Conversation</h3>
          <ChatPane principal={principal} setPrincipal={setPrincipal} people={people} pending={pending} away={away}
                    version={version} onChange={onChange} compact />
        </div>
      </div>
    </section>
  );
}

type Facts = {
  deal: string;
  rep: string;
  field: string;
  original: TraceNode;
  current: TraceNode;
  chosen: { node: TraceNode; field: string } | null;
  outranked: { node: TraceNode; field: string; effect: number } | null;
  rule: string;
  ruleText: string | null;
  claimed: number | null;
  disputed: boolean;
};

function caseFacts(trace: Trace, items: TimelineItem[]): Facts | null {
  const nodes = new Map(trace.nodes.map((n) => [n.id, n]));
  const original = trace.root ? nodes.get(trace.root) : undefined;
  if (!original || original.kind !== "line") return null;
  // Follow "corrected by" to the line that is live now.
  let current = original;
  for (let hop = 0; hop < 10; hop++) {
    const next = trace.edges.find((e) => e.from === current.id && e.label === "corrected by");
    if (!next || !nodes.get(next.to)) break;
    current = nodes.get(next.to)!;
  }
  const disputes = items.filter((i) => i.kind === "DECISION" && i.text.includes(` dispute ${original.id}:`));
  const asserted = disputes.map((i) => i.text.match(/\((\w+) asserted /)?.[1]).find(Boolean);
  const resolutions = trace.nodes.filter((n) => n.kind === "resolution");
  const effect = (n: TraceNode) =>
    Math.max(0, ...((n.disagreements as { effect_cents: number | null }[]) ?? []).map((d) => Math.abs(d.effect_cents ?? 0)));
  const resolution =
    resolutions.find((n) => n.field === asserted) ?? resolutions.slice().sort((a, b) => effect(b) - effect(a))[0];
  if (!resolution) return null;
  const field = String(resolution.field);
  const used = trace.edges.find((e) => e.from === resolution.id && e.label === "chosen");
  const alternatives = (resolution.disagreements as { source: string; basis: string; effect_cents: number | null }[]) ?? [];
  const biggest = alternatives.slice().sort((a, b) => Math.abs(b.effect_cents ?? 0) - Math.abs(a.effect_cents ?? 0))[0];
  const chosenNode = used ? nodes.get(used.to) : undefined;
  const altNode = biggest ? nodes.get(biggest.source) : undefined;
  const rule = String(resolution.rule ?? "");
  // The analyst's reply states the rule in words; use the first one that names it.
  const ruleText =
    items
      .filter((i) => i.kind === "REPLY")
      .flatMap((i) => i.text.split("\n"))
      .find((l) => l.startsWith(`Rule: ${rule}:`))
      ?.slice(`Rule: ${rule}: `.length) ?? null;
  return {
    deal: String(original.deal),
    rep: String(original.rep),
    field,
    original,
    current,
    chosen: chosenNode ? { node: chosenNode, field: highlightField(chosenNode, field, "chosen") } : null,
    outranked: altNode && biggest
      ? { node: altNode, field: highlightField(altNode, field, "alternative"), effect: biggest.effect_cents ?? 0 }
      : null,
    rule,
    ruleText,
    claimed: biggest && typeof original.amount_cents === "number" ? original.amount_cents + (biggest.effect_cents ?? 0) : null,
    disputed: disputes.length > 0,
  };
}

function highlightField(node: TraceNode, resolves: string, role: string): string {
  const marks = (node.highlight as { field: string; role: string; resolves: string }[]) ?? [];
  return marks.find((m) => m.resolves === resolves && m.role === role)?.field ?? "";
}

function display(field: string, value: unknown): string {
  if (value === null || value === undefined) return "";
  if (field.endsWith("_cents") && typeof value === "number") return usd(value);
  if (field === "term_months") return `${value} months`;
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}/.test(value)) return pt(value);
  return typeof value === "string" ? value : JSON.stringify(value);
}

function Evidence({ facts }: { facts: Facts }) {
  return (
    <div className="case-col">
      <h3>Evidence</h3>
      <div className="rule-banner">
        <span className="muted">The rule that decides</span>
        <strong>
          {facts.rule}
          {facts.ruleText ? `: ${facts.ruleText}` : `: ${DISPUTED[facts.field] ?? facts.field} comes from the ${facts.chosen ? sourceKind(facts.chosen.node) : "first source"}`}
        </strong>
      </div>
      <div className="evidence">
        {facts.outranked && (
          <SourceCard node={facts.outranked.node} field={facts.outranked.field} role="outranked"
                      note={`Would pay ${usd(facts.outranked.effect)} more if it counted`} />
        )}
        {facts.chosen && <SourceCard node={facts.chosen.node} field={facts.chosen.field} role="used" note="Used for the commission" />}
      </div>
    </div>
  );
}

function sourceKind(node: TraceNode): string {
  const table = String(node.table ?? "");
  return table === "deals" ? "CRM" : table === "contracts" ? "signed contract" : table.replace(/_/g, " ");
}

function SourceCard({ node, field, role, note }: { node: TraceNode; field: string; role: "used" | "outranked"; note: string }) {
  const snippet = (node.snippet as Record<string, unknown>) ?? {};
  const table = String(node.table ?? "");
  const title = table === "deals" ? "CRM opportunity" : table === "contracts" ? "E-signature · signed contract" : table;
  const system = String(snippet.source ?? "");
  const file = node.source_file ? `${String(node.source_file)} · row ${String(node.source_row ?? "?")}` : null;
  const shown = ["account", "crm_amount_cents", "acv_cents", "term_months", "owner_id"].filter((k) => k in snippet && k !== field);
  return (
    <div className={`card source ${role}`}>
      <div className="card-head">
        <strong>{title}</strong>
        <span className={`tag ${role === "used" ? "tag-used" : ""}`}>{role === "used" ? "used" : "outranked"}</span>
        {system && <span className="muted">from {system}</span>}
      </div>
      <div className="source-key">
        <span className="muted">{FIELD_NAMES[field] ?? field}</span>
        <span className="source-value">{display(field, snippet[field])}</span>
      </div>
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
        {node.source_line ? <pre className="raw">{String(node.source_line)}</pre> : null}
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
          <Money cents={facts.current.amount_cents} trace={facts.current.id} />
        </div>
        <div className="figure-sub">
          {changed ? (
            <>
              paid as claimed, by exception · was <Money cents={facts.original.amount_cents} trace={facts.original.id} /> under the plan
            </>
          ) : facts.claimed !== null ? (
            <>
              vs <span className="claimed">{usd(facts.claimed)}</span> {facts.disputed ? "claimed" : "if the outranked record counted"}
              {facts.disputed && <> · difference {usd(facts.claimed - (facts.original.amount_cents ?? 0))}</>}
            </>
          ) : null}
        </div>
        <div className="figure-status">
          <span className="muted">
            {String(facts.current.quarter)} · line {facts.current.id} · {String(facts.current.status)}
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

const KEEP_DECISIONS = /^D-\d+ (commission_calc|dispute|escalation|resubmission_check|integrity_check)\b/;

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
    const hit = item.refs.some((r) => ids.has(r)) || (item.kind === "APPROVAL" && / plan_exception /.test(item.text) && dealWord.test(item.text));
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

  for (const a of pending.filter((p) => ids.has(p.id))) {
    steps.push({ at: a.since, ids: [a.id], label: <>Waiting on {name(a.approver)}</>, detail: `${usd(a.amount_cents)} to decide`, tone: "human" });
  }
  return steps;
}

function times(n: number): string {
  return n === 2 ? "twice" : `${n} times`;
}
