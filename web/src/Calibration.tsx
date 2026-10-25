import { useState, type ReactNode } from "react";
import { api, type Calibration, type Message, type Principal } from "./api";
import { Money, Reply, Rich, When, personName, sourceName } from "./format";
import { ConfidenceBadge, Drawer, Factors, Unavailable, pct, useApi } from "./ui";
import "./calibration.css";

// D5: how Quota settled one commission line. What every source says, the written rule that settled each
// disagreement, the amount, how sure it is and why, and what happens next. Every figure is the server's.

type Props = {
  line: string;
  title: string;
  back: string;
  people: Principal[];
  version: number;
  onChange: () => void;
  rep?: string;
  customer?: string;
};

const SOURCES: Record<string, string> = {
  crm: "CRM",
  contract: "Contract",
  contracts: "Contract",
  esign: "E-signature",
  esign_events: "E-signature",
  e_signature: "E-signature",
  "e-signature": "E-signature",
  billing: "Billing",
  billing_events: "Billing",
  stripe: "Billing",
  hr: "HR",
  people: "HR",
  messages: "Messages",
  email: "Messages",
  slack: "Messages",
};

function sourceLabel(s: string): string {
  return SOURCES[s.toLowerCase()] ?? s.charAt(0).toUpperCase() + s.slice(1).replace(/_/g, " ");
}

const MONEY_FIELD = /cents|amount|acv|price|value_usd|base|invoice|payment|refund/i;
const DATE = /^\d{4}-\d{2}(?:-\d{2})?(?:T[\d:.]+Z?)?$/;

/** A value as a source wrote it: dollars open the trace, dates in Pacific, an object as its labeled fields. */
function Value({ v, field, trace }: { v: unknown; field?: string; trace: string }): ReactNode {
  if (v === null || v === undefined || v === "") return <span className="muted">not stated</span>;
  if (typeof v === "number") return MONEY_FIELD.test(field ?? "") ? <Money cents={v} trace={trace} /> : <span className="num">{v.toLocaleString("en-US")}</span>;
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "string") return DATE.test(v) ? <When ts={v} date /> : <Rich text={v} trace={trace} />;
  if (Array.isArray(v)) {
    return v.map((x, i) => (
      <span key={i}>
        {i > 0 && " · "}
        <Value v={x} field={field} trace={trace} />
      </span>
    ));
  }
  if (typeof v === "object") {
    const o = v as Record<string, unknown>;
    // An outranked alternative is usually {source, value}: say whose value it was.
    if ("value" in o && ("source" in o || "ref" in o)) {
      return (
        <span>
          {typeof o.basis === "string" && <>{o.basis.replace(/_/g, " ")} </>}
          <Value v={o.value} field={typeof o.field === "string" ? o.field : field} trace={trace} />
          {typeof o.source === "string" && (
            <span className="muted"> ({o.source.includes(":") ? sourceName(o.source) : sourceLabel(o.source)})</span>
          )}
          {typeof o.ref === "string" && <span className="mono muted"> {o.ref}</span>}
        </span>
      );
    }
    return (
      <span className="calib-kv">
        {Object.entries(o).map(([k, x]) => (
          <span key={k}>
            <span className="muted">
              <Rich text={k} />
            </span>{" "}
            <Value v={x} field={k} trace={trace} />
          </span>
        ))}
      </span>
    );
  }
  return String(v);
}

export function CalibrationDrawer({ line, title, back, people, version, onChange, rep, customer }: Props) {
  const [tick, setTick] = useState(0);
  const cal = useApi<Calibration>(() => api.calibration(line), [line, version, tick]);
  const msgs = useApi<Message[]>(() => (rep ? api.messages(rep) : Promise.resolve([])), [rep, version, tick]);
  const answered = () => {
    setTick((t) => t + 1);
    onChange();
  };

  return (
    <Drawer title={title} back={back} wide>
      <div className="calib">
        {cal.missing ? (
          <Unavailable what="The calibration panel" path={`/api/lines/${line}/calibration`} />
        ) : cal.error ? (
          <p className="error">{cal.error}</p>
        ) : !cal.data ? (
          <p className="muted">Loading how Quota settled {line}…</p>
        ) : (
          <Panel c={cal.data} line={line} people={people} onAnswered={answered} />
        )}
        {rep && <Thread line={line} rep={rep} customer={customer} people={people} messages={msgs.data} missing={msgs.missing} error={msgs.error} />}
      </div>
    </Drawer>
  );
}

function Panel({ c, line, people, onAnswered }: { c: Calibration; line: string; people: Principal[]; onAnswered: () => void }) {
  const a = c.amount;
  const total = a.total ?? (a.total_cents as number | undefined);
  const base = a.base_portion ?? (a.base_portion_cents as number | undefined);
  const accel = a.accel_portion ?? (a.accel_portion_cents as number | undefined);
  return (
    <>
      <section className="calib-sec">
        <h4>What each source says</h4>
        {c.evidence.length === 0 ? (
          <p className="muted">No source evidence on this line.</p>
        ) : (
          <table className="table calib-evidence">
            <tbody>
              {c.evidence.map((e, i) => (
                <tr key={`${e.source}-${e.field ?? ""}-${i}`}>
                  <td className="calib-src">
                    {sourceLabel(e.source)}
                    {e.field && (
                      <div className="muted">
                        <Rich text={e.field} />
                      </div>
                    )}
                  </td>
                  <td>
                    <Value v={e.value} field={e.field} trace={line} />
                  </td>
                  <td className={`calib-agree ${e.agrees === true ? "yes" : e.agrees === false ? "no" : "none"}`}>
                    {e.agrees === true ? "✓ agrees" : e.agrees === false ? "! disagrees" : (e.label ?? "—")}
                  </td>
                  <td className="mono muted calib-ref">{e.ref}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="calib-sec">
        <h4>How disagreements were settled</h4>
        {c.rules.length === 0 ? (
          <p className="muted">Every source agreed; no rule was needed.</p>
        ) : (
          <ul className="calib-rules">
            {c.rules.map((r, i) => (
              <li key={`${r.field}-${i}`}>
                <div>
                  <strong>
                    <Rich text={r.field} />
                  </strong>{" "}
                  settled by <span className="mono">{r.rule}</span>: <Value v={r.chosen} field={r.field} trace={line} />
                </div>
                {r.alternatives && r.alternatives.length > 0 && (
                  <div className="muted">
                    outranked:{" "}
                    {r.alternatives.map((alt, j) => (
                      <span key={j}>
                        {j > 0 && " · "}
                        <Value v={alt} field={r.field} trace={line} />
                      </span>
                    ))}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="calib-sec">
        <h4>Computed amount</h4>
        <div className="calib-total">
          <Money cents={total} trace={line} />
        </div>
        <dl className="calib-parts">
          {base !== undefined && (
            <div>
              <dt>Credit at the base rate</dt>
              <dd>
                <Money cents={base} trace={line} />
              </dd>
            </div>
          )}
          {accel !== undefined && (
            <div>
              <dt>Credit above quota</dt>
              <dd>
                <Money cents={accel} trace={line} />
              </dd>
            </div>
          )}
          {a.rate !== undefined && a.rate !== null && (
            <div>
              <dt>Rate</dt>
              <dd>{typeof a.rate === "number" ? pct(a.rate) : a.rate}</dd>
            </div>
          )}
        </dl>
      </section>

      <section className="calib-sec">
        <h4>
          Confidence <ConfidenceBadge score={c.confidence.score} band={c.confidence.band} />
        </h4>
        <Factors confidence={c.confidence} />
      </section>

      <Outcome c={c} line={line} people={people} onAnswered={onAnswered} />
    </>
  );
}

function Outcome({ c, line, people, onAnswered }: { c: Calibration; line: string; people: Principal[]; onAnswered: () => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const o = c.outcome;
  const asked = o.asked ?? null;
  const held = o.status === "held" || !!asked;

  async function answer(decision: "approve" | "reject" | "reply") {
    if (!asked) return;
    setBusy(true);
    setError(null);
    try {
      // `asked` is the Needs-you item itself; its id is the approval or hold the answer goes to.
      const res = await api.answer(asked.id ?? asked.item ?? line, asked.person, decision, text);
      if (!res.ok) {
        setError(typeof res.refused === "string" ? res.refused : `Refused: ${JSON.stringify(res.refused ?? res.result)}`);
        return;
      }
      setText("");
      onAnswered();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className={`calib-sec calib-outcome ${held ? "held" : ""}`}>
      <h4>Outcome</h4>
      {o.text && (
        <p className="calib-outcome-text">
          <Rich text={o.text} trace={line} />
        </p>
      )}
      {o.approval && (
        <p className="muted">
          Approval <Rich text={o.approval} />
        </p>
      )}
      {asked && (
        <>
          <div className="calib-asked">
            <strong>Asked {personName(asked.person, people)}:</strong> <Reply text={asked.question} trace={line} />
          </div>
          <div className="calib-answer">
            <textarea value={text} onChange={(e) => setText(e.target.value)} rows={1}
                      placeholder={`Answer as ${personName(asked.person, people)} (a reason, or a reply)`} aria-label="Answer" />
            <div className="calib-actions">
              <button className="primary" disabled={busy} onClick={() => answer("approve")}>
                Approve
              </button>
              <button disabled={busy} onClick={() => answer("reject")}>
                Reject
              </button>
              <button disabled={busy || !text.trim()} onClick={() => answer("reply")}>
                Reply
              </button>
            </div>
          </div>
          {error && <p className="error">{error}</p>}
        </>
      )}
    </section>
  );
}

function Thread({ line, rep, customer, people, messages, missing, error }: {
  line: string;
  rep: string;
  customer?: string;
  people: Principal[];
  messages: Message[] | null;
  missing: boolean;
  error: string | null;
}) {
  const needle = customer?.toLowerCase();
  const shown = (messages ?? []).filter((m) => {
    const t = m.text.toLowerCase();
    return m.about === line || t.includes(line.toLowerCase()) || (!!needle && t.includes(needle));
  });
  return (
    <section className="calib-sec calib-thread">
      <h4>
        Dispute thread
        <a className="calib-case" href={`#case/${encodeURIComponent(line)}`}>
          Open the case →
        </a>
      </h4>
      {missing && <Unavailable what="Messages" path={`/api/messages?principal=${rep}`} />}
      {error && <p className="error">{error}</p>}
      {messages && shown.length === 0 && (
        <p className="muted">
          No messages from or to {personName(rep, people)} mention {customer ?? line}.
        </p>
      )}
      {shown.length > 0 && (
        <div className="thread">
          {shown.map((m) => (
            <div key={m.id} className={`bubble ${m.author.startsWith("seat:") ? "quota" : "theirs"}`}>
              <div className="bubble-meta">
                <span>{personName(m.author, people)}</span>
                <When ts={m.at} />
              </div>
              <div className="bubble-text">
                <Reply text={m.text} trace={m.about ?? line} />
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
