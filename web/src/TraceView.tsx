import { useEffect, useMemo, useState, type ReactNode } from "react";
import { provenance } from "./Case";
import { api, type Principal, type Trace, type TraceEdge, type TraceNode } from "./api";
import { Money, Rich, TraceLink, When, isTraceable, learnAccounts, personName, pt, sourceName, usd } from "./format";

const SECTIONS: [string, string][] = [
  ["line", "Commission lines"],
  ["je", "Journal entries"],
  ["decision", "Decisions, replayed from their recorded inputs"],
  ["resolution", "How each field was resolved"],
  ["input", "Source records, as they were when decided"],
  ["precedent", "Precedents"],
  ["approval", "Approvals"],
  ["payroll", "Payroll"],
  ["billing", "Billing"],
  ["deal", "Deals"],
  ["account", "Accounts"],
  ["message", "Messages"],
  ["refusal", "Refusals"],
  ["schedule", "Amortization"],
  ["event", "Events"],
];

type Props = { id: string; stops: string[]; people: Principal[]; onOpen: (id: string) => void };

export function TraceView({ id, stops, people, onOpen }: Props) {
  const [draft, setDraft] = useState(id);
  // The chosen instant itself, null meaning now: stops keep arriving as the timeline loads and grows.
  const [asOf, setAsOf] = useState<string | null>(null);
  const [trace, setTrace] = useState<Trace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const index = asOf === null ? stops.length : Math.max(0, stops.indexOf(asOf));

  useEffect(() => {
    setDraft(id);
  }, [id]);
  useEffect(() => {
    let live = true;
    setError(null);
    api
      .trace(id, asOf)
      .then((t) => {
        if (!live) return;
        learnAccounts(t.nodes);
        setTrace(t);
      })
      .catch((e: Error) => live && (setTrace(null), setError(e.message)));
    return () => {
      live = false;
    };
  }, [id, asOf, asOf === null ? stops.length : 0]);

  const root = trace?.nodes.find((n) => n.id === trace.root) ?? null;
  const grouped = useMemo(() => {
    const out = new Map<string, TraceNode[]>();
    for (const node of trace?.nodes ?? []) {
      if (node.id === trace?.root) continue;
      out.set(node.kind, [...(out.get(node.kind) ?? []), node]);
    }
    return out;
  }, [trace]);

  return (
    <section className="trace">
      <form
        className="trace-bar"
        onSubmit={(e) => {
          e.preventDefault();
          if (draft.trim()) onOpen(draft.trim().toUpperCase().startsWith("DEAL/") ? draft.trim() : draft.trim().toUpperCase());
        }}
      >
        <label htmlFor="trace-id">Trace</label>
        <input id="trace-id" value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="CL-0002, JE-0012, A-0002 …" />
        <button type="submit">Open</button>
      </form>

      <div className="asof">
        <div className="asof-head">
          <span>As of</span>
          <strong>{asOf ? <When ts={asOf} /> : "now"}</strong>
          {asOf && (
            <button className="link" onClick={() => setAsOf(null)}>
              back to now
            </button>
          )}
        </div>
        <input
          type="range"
          min={0}
          max={stops.length}
          value={index}
          onChange={(e) => {
            const i = Number(e.target.value);
            setAsOf(i >= stops.length ? null : stops[i]);
          }}
          aria-label="As-of time"
        />
        <div className="asof-scale">
          <When ts={stops[0]} />
          <span>now</span>
        </div>
      </div>

      {error && <p className="error">{error}</p>}
      {trace && !trace.known && (
        <div className="card unknown">
          <h3>{trace.requested} was not known yet</h3>
          <p>Nothing about it had reached the ledger by <When ts={trace.as_of} />. Slide the as-of time later.</p>
        </div>
      )}
      {root && <RootCard node={root} trace={trace!} people={people} />}
      {SECTIONS.filter(([kind]) => grouped.has(kind)).map(([kind, title]) => {
        const nodes = grouped.get(kind)!;
        const used = kind === "input" ? nodes.filter((n) => (n.highlight as unknown[]).length > 0) : nodes;
        const rest = kind === "input" ? nodes.filter((n) => (n.highlight as unknown[]).length === 0) : [];
        return (
          <div key={kind} className="trace-section">
            <h3>{title}</h3>
            <div className="cards">
              {used.map((node) => (
                <NodeCard key={node.id} node={node} trace={trace!} people={people} />
              ))}
            </div>
            {rest.length > 0 && (
              <details className="more">
                <summary>
                  {rest.length} more record{rest.length > 1 ? "s" : ""} the decision read (plan, earlier deals, e-sign log)
                </summary>
                <div className="cards">
                  {rest.map((node) => (
                    <NodeCard key={node.id} node={node} trace={trace!} people={people} />
                  ))}
                </div>
              </details>
            )}
          </div>
        );
      })}
    </section>
  );
}

// How an edge reads from the other end ("CL-0002 calculated in D-0005" -> on D-0005: "calculates CL-0002").
const REVERSE: Record<string, string> = {
  "calculated in": "calculates",
  "decided by": "decides",
  "decided in": "decides",
  "approved by": "approves",
  approval: "approves",
  read: "read by",
  resolved: "resolved in",
  chosen: "chosen for",
  alternative: "outranked in",
  precedent: "applied in",
  entry: "entry for",
  about: "subject of",
  "routed from": "routed to",
  "routed to": "routed from",
  made: "made by",
  corrects: "corrected by",
  "corrected by": "corrects",
  "in payroll": "pays",
  pays: "in payroll",
  source: "source of",
  "posted in": "posts to",
  line: "on deal",
  is: "is",
  "refused on": "refused",
  amortizes: "amortized by",
};

function links(node: TraceNode, edges: TraceEdge[]): ReactNode {
  const out = edges
    .filter((e) => e.from === node.id && !(node.kind === "decision" && e.label === "read"))
    .map((e) => ({ label: e.label, other: e.to }));
  const into = edges.filter((e) => e.to === node.id).map((e) => ({ label: REVERSE[e.label] ?? `${e.label} by`, other: e.from }));
  const all = [...out, ...into];
  if (all.length === 0) return null;
  return (
    <div className="edges">
      {all.map((e, i) => (
        <span key={i} className="edge">
          {e.label} {isTraceable(e.other) ? <TraceLink id={e.other} /> : <span className="mono">{e.other}</span>}
        </span>
      ))}
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  if (children === null || children === undefined || children === "") return null;
  return (
    <div className="field">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function str(v: unknown): string {
  return v === null || v === undefined ? "" : typeof v === "string" ? v : JSON.stringify(v);
}

/** A source value as recorded; cents also read as dollars, and long JSON folds. */
function Value({ name, value }: { name: string; value: unknown }) {
  const text = str(value);
  if (name.endsWith("_cents") && typeof value === "number") {
    return (
      <>
        {text} <span className="muted">({usd(value)})</span>
      </>
    );
  }
  if (text.length > 120) {
    return (
      <details className="fold">
        <summary>{text.slice(0, 60)}…</summary>
        {text}
      </details>
    );
  }
  return <>{text}</>;
}

function fieldValue(field: string, value: unknown): string {
  if ((field === "base" || field === "amount") && typeof value === "number") return usd(value);
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}/.test(value) ? pt(value, true) : str(value);
}

function RootCard({ node, trace, people }: { node: TraceNode; trace: Trace; people: Principal[] }) {
  return (
    <div className={`card root kind-${node.kind}`}>
      <div className="root-head">
        <span className="kind-tag">{node.kind}</span>
        <h2>{node.label}</h2>
        {typeof node.amount_cents === "number" && <div className="big">{usd(node.amount_cents)}</div>}
      </div>
      <Body node={node} people={people} />
      {links(node, trace.edges)}
    </div>
  );
}

function NodeCard({ node, trace, people }: { node: TraceNode; trace: Trace; people: Principal[] }) {
  const pending = node.kind === "approval" && node.decision === "pending";
  return (
    <div className={`card kind-${node.kind} ${pending ? "human" : ""}`}>
      <div className="card-head">
        {isTraceable(node.id) ? (
          <TraceLink id={node.id} />
        ) : node.kind === "input" ? (
          <strong title={node.id}>{sourceName(node.id)}</strong>
        ) : (
          <span className="mono">{node.id}</span>
        )}
        {typeof node.amount_cents === "number" && <Money cents={node.amount_cents} trace={isTraceable(node.id) ? node.id : null} />}
        {pending && <span className="badge accent">waiting on {personName(str(node.approver), people)}</span>}
      </div>
      <Body node={node} people={people} />
      {links(node, trace.edges)}
    </div>
  );
}

function Body({ node, people }: { node: TraceNode; people: Principal[] }) {
  const n = node as Record<string, unknown>;
  switch (node.kind) {
    case "line":
      return (
        <dl>
          <Field label="Rep / deal">{`${str(n.rep)} / ${str(n.deal)}`}</Field>
          <Field label="Quarter">{str(n.quarter)}</Field>
          <Field label="Credit">{usd(n.credit_cents as number)}</Field>
          <Field label="Status">{str(n.status)}</Field>
          <Field label="Known at"><When ts={str(n.known_at)} /></Field>
        </dl>
      );
    case "je": {
      const lines = (n.lines as { account: string; name: string; debit_cents: number; credit_cents: number; subledger_ref: string }[]) ?? [];
      return (
        <>
          <dl>
            <Field label="Effective">{`${str(n.effective_date)} (period ${str(n.period)})`}</Field>
            <Field label="Status">{str(n.status)}</Field>
            <Field label="Drafted by">{personName(str(n.created_by), people)}</Field>
          </dl>
          <table className="je">
            <tbody>
              {lines.map((l, i) => (
                <tr key={i}>
                  <td className="mono">{l.account}</td>
                  <td>{l.name}</td>
                  <td className="num">{l.debit_cents ? usd(l.debit_cents) : ""}</td>
                  <td className="num">{l.credit_cents ? usd(l.credit_cents) : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      );
    }
    case "decision":
      return (
        <>
          <p className="explain">
            <Rich text={str(n.explanation)} trace={node.id} />
          </p>
          <dl>
            <Field label="Function">{<code>{str(n.fn)}</code>}</Field>
            <Field label="Outcome">{str(n.outcome)}</Field>
            <Field label="Seat">{personName(str(n.seat), people)}</Field>
            <Field label="Model">{n.model ? str(n.model) : "none (code decided)"}</Field>
            <Field label="Replay">
              <span className={`replay ${n.replay === "MATCH" ? "ok" : "bad"}`}>
                {n.replay === "MATCH" ? "✓ recomputed from recorded inputs: matches stored output" : "✗ MISMATCH"}
              </span>
            </Field>
          </dl>
        </>
      );
    case "resolution": {
      const alternatives = (n.disagreements as { source: string; value: unknown; basis: string; effect_cents: number | null }[]) ?? [];
      return (
        <>
          <p className="resolved">
            <strong>{str(n.field).replace(/_/g, " ")}</strong> = <span className="mono">{fieldValue(str(n.field), n.value)}</span>
          </p>
          <dl>
            <Field label="Rule">{str(n.rule)}</Field>
            <Field label="Chosen">{`${str(n.basis).replace(/_/g, " ")} from the ${sourceName(str(n.source))}`}</Field>
          </dl>
          {alternatives.length > 0 && (
            <ul className="alts">
              {alternatives.map((a, i) => (
                <li key={i}>
                  outranked: {a.basis.replace(/_/g, " ")} <span className="mono">{fieldValue(str(n.field), a.value)}</span> ({sourceName(a.source)})
                  {a.effect_cents !== null && a.effect_cents !== undefined && <> · would change pay by {usd(a.effect_cents)}</>}
                </li>
              ))}
            </ul>
          )}
        </>
      );
    }
    case "input": {
      const snippet = (n.snippet as Record<string, unknown>) ?? {};
      const highlight = (n.highlight as { field: string; role: string; resolves: string }[]) ?? [];
      const origin = provenance(node);
      return (
        <>
          {origin && (
            <details className="details origin">
              <summary>
                {origin.file} · row {origin.row}
              </summary>
              {origin.raw && <pre className="raw">{origin.raw}</pre>}
            </details>
          )}
          <table className="snippet">
            <tbody>
              {Object.entries(snippet).map(([k, v]) => {
                const marks = highlight.filter((h) => h.field === k);
                const role = marks.some((m) => m.role === "chosen") ? "chosen" : marks.length ? "alternative" : "";
                return (
                  <tr key={k} className={role ? `hl-${role}` : ""}>
                    <th>{k}</th>
                    <td>
                      <span className="val">
                        <Value name={k} value={v} />
                      </span>
                      {marks.map((m, i) => (
                        <span key={i} className={`mark ${m.role}`}>
                          {m.role === "chosen" ? "used for" : "outranked for"} {m.resolves}
                        </span>
                      ))}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <dl>
            <Field label="Known at"><When ts={str(n.known_at)} /></Field>
            <Field label="Later">{n.newer_version ? `a newer version exists: ${str(n.newer_version)}` : ""}</Field>
          </dl>
          <details className="details">
            <summary>Details</summary>
            <p className="muted mono">
              {node.id}
              {" "}· sha256 {str(n.sha256)}
            </p>
          </details>
        </>
      );
    }
    case "precedent":
      return (
        <dl>
          <Field label="Scope">{str(n.scope)}</Field>
          <Field label="Override">{str(n.override)}</Field>
          <Field label="In force">{`${pt(str(n.effective_from))} to ${pt(str(n.expires_at), true)}`}</Field>
        </dl>
      );
    case "approval":
      return (
        <dl>
          <Field label="For">
            <>
              {str(n.approval_kind)} on {isTraceable(str(n.subject)) ? <TraceLink id={str(n.subject)} /> : str(n.subject)}
            </>
          </Field>
          <Field label="Approver">{personName(str(n.approver), people)}</Field>
          <Field label="Requested by">{personName(str(n.requested_by), people)}</Field>
          <Field label="Decision">{n.decision === "pending" ? "pending" : `${str(n.decision)} → ${str(n.result)}`}</Field>
          <Field label="Reason">{str(n.reason)}</Field>
          <Field label="Decided at"><When ts={str(n.decided_at)} /></Field>
        </dl>
      );
    case "payroll":
      return (
        <dl>
          <Field label="Period">{str(n.period)}</Field>
          <Field label="Status">{str(n.status)}</Field>
        </dl>
      );
    case "billing": {
      const ref = str(n.external_ref);
      const stripe = /^(in_|pi_|re_)/.test(ref);
      return (
        <dl>
          <Field label="At"><When ts={str(n.at)} /></Field>
          <Field label={stripe ? "Stripe id" : "External ref"}>{<span className="mono">{ref}</span>}</Field>
        </dl>
      );
    }
    case "message":
      return (
        <>
          <p className="quote">“{str(n.text)}”</p>
          <dl>
            <Field label="From → to">{`${personName(str(n.author), people)} → ${personName(str(n.to), people)}`}</Field>
          </dl>
        </>
      );
    case "refusal":
      return (
        <>
          <div className="codes">
            {((n.codes as string[]) ?? []).map((c) => (
              <code key={c} className="code">
                {c}
              </code>
            ))}
          </div>
          <dl>
            <Field label="Who tried">{personName(str(n.actor), people)}</Field>
            <Field label="Action">{`${str(n.action)} ${str(n.subject)}`}</Field>
            <Field label="Detail">{str(n.detail)}</Field>
          </dl>
        </>
      );
    case "schedule":
      return (
        <dl>
          <Field label="Months">{`${str(n.months)} × ${usd(n.monthly_cents as number)} from ${str(n.start)}`}</Field>
          <Field label="Status">{str(n.status)}</Field>
        </dl>
      );
    case "deal": {
      const snippet = (n.snippet as Record<string, unknown>) ?? {};
      return (
        <dl>
          {Object.entries(snippet).map(([k, v]) => (
            <Field key={k} label={k}>
              {str(v)}
            </Field>
          ))}
        </dl>
      );
    }
    default:
      return (
        <dl>
          {Object.entries(n)
            .filter(([k]) => !["id", "kind", "label", "amount_cents"].includes(k))
            .map(([k, v]) => (
              <Field key={k} label={k}>
                {str(v)}
              </Field>
            ))}
        </dl>
      );
  }
}
