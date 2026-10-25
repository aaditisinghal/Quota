import { useEffect, useState, type ReactNode } from "react";
import { api, isMissing, type Intake, type Mapping, type Principal, type ReviewItem } from "./api";
import { FlashMoney } from "./Flash";
import { Money, Reply, personName, pt, usd } from "./format";

// R7.7: what came in from the inbox and how it was read. The backtest is the proof number, so it comes first;
// then the review queue (what a person must answer before those rows load), each file with its mapping, the
// plans read from PDF with the sentence each term came from, and the intake log.

type Props = { people: Principal[]; version: number; onChange: () => void };

export function IntakeView({ people, version, onChange }: Props) {
  const [intake, setIntake] = useState<Intake | null>(null);
  const [missing, setMissing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .intake()
      .then((i) => {
        setIntake(i);
        setMissing(false);
      })
      .catch((e) => (isMissing(e) ? setMissing(true) : setError((e as Error).message)));
  }, [version]);

  async function resolve(item: ReviewItem, resolution: Record<string, unknown>) {
    setError(null);
    try {
      setIntake(await api.review(item.id, resolution));
      onChange();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  if (missing) {
    return (
      <section className="intake">
        <div className="card unknown">
          <h3>No inbox on this server</h3>
          <p className="muted">
            Serve with <code>python -m quota serve --world fernhill --seed 7 --from-inbox</code> to read the exports.
          </p>
        </div>
      </section>
    );
  }
  if (!intake) return <section className="intake muted">{error ?? "Loading the inbox…"}</section>;

  // An item can be raised more than once in a run; it is one question.
  const items = [...new Map(intake.review_items.map((i) => [i.id, i])).values()];
  const log = [...new Set(intake.log)];
  const bt = intake.backtest;
  const facts = Object.values(intake.loaded).reduce((a, b) => a + b, 0);

  return (
    <section className="intake">
      {error && <p className="error">{error}</p>}
      <div className="panel">
        <h2>Backtest, {bt.quarter}</h2>
        <p className="headline">{bt.headline}</p>
        <div className="tiles">
          <Tile value={`${bt.matched} / ${bt.rows}`} label="payouts reproduced" />
          <Tile value={String(bt.errors)} label="spreadsheet errors" note="held for the Controller" />
          <Tile value={<FlashMoney id="intake:overpaid" cents={bt.overpaid_cents} />} label="overpaid" />
          <Tile value={String(bt.unexplained)} label="unexplained" />
        </div>
        {bt.findings.length > 0 && (
          <table className="table findings">
            <thead>
              <tr>
                <th>Source</th>
                <th>Error</th>
                <th>Rep / deal</th>
                <th className="num">Paid</th>
                <th className="num">Contract pays</th>
                <th className="num">Overpaid</th>
                <th>Held for</th>
              </tr>
            </thead>
            <tbody>
              {bt.findings.map((f) => (
                <tr key={f.id}>
                  <td className="mono">
                    {f.file} · row {f.row}
                  </td>
                  <td>
                    <span className="tag">{f.class.replace(/_/g, " ")}</span>
                  </td>
                  <td>
                    {personName(f.rep, people)} / <a className="ref" href={`#trace/deal/${encodeURIComponent(f.deal)}`}>{f.deal}</a>
                  </td>
                  <td className="num">{usd(f.paid_cents)}</td>
                  <td className="num">{usd(f.correct_cents)}</td>
                  <td className="num">
                    <Money cents={f.overpaid_cents} />
                  </td>
                  <td>{personName(f.routed_to, people)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className={`panel ${items.length ? "human-panel" : ""}`}>
        <h2>
          Review queue <span className="muted">{items.length ? `${items.length} waiting on people` : "nothing to answer"}</span>
        </h2>
        {items.map((item) => (
          <ReviewCard key={item.id} item={item} people={people} onResolve={(r) => resolve(item, r)} />
        ))}
      </div>

      <div className="panel">
        <h2>
          Files <span className="muted">{facts} facts loaded, each with its file and row</span>
        </h2>
        <table className="table">
          <thead>
            <tr>
              <th>File</th>
              <th>Read as</th>
              <th className="num">Rows</th>
              <th>Mapping</th>
              <th>Loaded</th>
            </tr>
          </thead>
          <tbody>
            {intake.files.map((f) => (
              <tr key={f.file}>
                <td>
                  <span className="mono">{f.file}</span>
                  {f.mappings.length > 0 && <MappingTable mappings={f.mappings} />}
                </td>
                <td>{f.kind?.replace(/_/g, " ") ?? <span className="bad">unknown</span>}</td>
                <td className="num">{f.rows}</td>
                <td>
                  <span className="tag">{f.mapping_source === "reused" ? "reused" : f.mapping_source ? "proposed" : "none"}</span>{" "}
                  {f.mapping_source && f.mapping_source !== "reused" && <span className="muted">by {f.mapping_source}</span>}
                </td>
                <td className={f.loaded ? "ok" : "bad"}>{f.loaded ? "✓ loaded" : "held for review"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {Object.keys(intake.plans).length > 0 && (
        <div className="panel">
          <h2>Plans read from PDF</h2>
          <div className="cards">
            {Object.entries(intake.plans).map(([rep, p]) => (
              <div key={rep} className="card">
                <div className="card-head">
                  <strong>{personName(rep, people)}</strong>
                  <span className="mono muted">{p.file}</span>
                </div>
                <dl>
                  {Object.entries(p.terms ?? {}).map(([key, t]) => (
                    <div key={key} className="field" title={t.quote}>
                      <dt>{key.replace(/_/g, " ")}</dt>
                      <dd>
                        {t.value === null || t.value === undefined ? (
                          <span className="bad">unclear</span>
                        ) : typeof t.value === "string" && /^\d{4}-\d{2}-\d{2}/.test(t.value) ? (
                          pt(t.value, true)
                        ) : (
                          String(t.value)
                        )}{" "}
                        <span className="muted">§{t.section}</span>
                      </dd>
                    </div>
                  ))}
                </dl>
              </div>
            ))}
          </div>
        </div>
      )}

      <details className="panel more">
        <summary>Intake log ({log.length} lines)</summary>
        <ul className="log">
          {log.map((l) => (
            <li key={l} className="mono">
              {l}
            </li>
          ))}
        </ul>
      </details>
    </section>
  );
}

function Tile({ value, label, note }: { value: ReactNode; label: string; note?: string }) {
  return (
    <div className="tile">
      <div className="tile-value">{value}</div>
      <div className="tile-label">{label}</div>
      {note && <div className="tile-note">{note}</div>}
    </div>
  );
}

function MappingTable({ mappings }: { mappings: Mapping[] }) {
  return (
    <details className="details">
      <summary>{mappings.length} columns mapped</summary>
      <table className="table mapping">
        <thead>
          <tr>
            <th>Column</th>
            <th>Field</th>
            <th>Read as</th>
            <th className="num">Confidence</th>
            <th className="num">Parsed</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {mappings.map((m) => (
            <tr key={m.source_column} className={m.status === "approved" ? "" : "changed"}>
              <td className="mono">{m.source_column}</td>
              <td className="mono">{m.canonical_field}</td>
              <td>{m.transform.replace(/_/g, " ")}</td>
              <td className="num">{Math.round(m.confidence * 100)}%</td>
              <td className="num">{m.parse_rate === undefined ? "" : `${Math.round(m.parse_rate * 100)}%`}</td>
              <td className={m.status === "approved" ? "ok" : "bad"} title={(m.problems ?? []).join("; ")}>
                {m.status === "approved" ? "✓" : m.status}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

/** One question for a person, with the answer that resolves it. */
function ReviewCard({ item, people, onResolve }: { item: ReviewItem; people: Principal[]; onResolve: (r: Record<string, unknown>) => void }) {
  const reps = people.filter((p) => p.capabilities.includes("dispute:own_lines"));
  // An envelope id names its deal ("ENV-willow-robotics"); offer that as the answer to check.
  const [answer, setAnswer] = useState(() => (item.kind === "NO_OPPORTUNITY" ? String(item.envelope_id ?? "").replace(/^ENV-/, "") : ""));
  const [busy, setBusy] = useState(false);
  const send = async (resolution: Record<string, unknown>) => {
    setBusy(true);
    try {
      await onResolve(resolution);
    } finally {
      setBusy(false);
    }
  };
  const where = item.file ? `${item.file}${item.row ? ` · row ${item.row}` : ""}` : "";

  return (
    <div className="card human review-card">
      <div className="card-head">
        <span className="tag">{item.kind.replace(/_/g, " ").toLowerCase()}</span>
        <span className="mono muted">{where}</span>
        <span className="muted">for {personName(item.routed_to, people)}</span>
      </div>
      <p className="review-question">
        <Reply text={item.question} />
      </p>
      {typeof item.quote === "string" && <p className="quote">“{item.quote}”</p>}
      <div className="actions">
        {item.kind === "BLANK_OWNER" && (
          <>
            <select value={answer} onChange={(e) => setAnswer(e.target.value)} aria-label="Owner">
              <option value="">Choose the owner…</option>
              {reps.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
            <button className="primary" disabled={busy || !answer} onClick={() => send({ owner: answer })}>
              Approve
            </button>
          </>
        )}
        {item.kind === "NO_OPPORTUNITY" && (
          <>
            <input value={answer} onChange={(e) => setAnswer(e.target.value)} placeholder="opportunity id"
                   aria-label="Opportunity id" />
            <button className="primary" disabled={busy || !answer.trim()} onClick={() => send({ deal_id: answer.trim() })}>
              Approve
            </button>
          </>
        )}
        {item.kind === "AMBIGUOUS" && item.key === "booking_date" && (
          <>
            <button className="primary" disabled={busy} onClick={() => send({ [String(item.key)]: "countersignature" })}>
              Countersignature
            </button>
            <button disabled={busy} onClick={() => send({ [String(item.key)]: "customer_signature" })}>
              Customer's signature
            </button>
          </>
        )}
        {item.kind === "MAPPING" && (
          <button className="primary" disabled={busy}
                  onClick={() => send({
                    mappings: Object.fromEntries(((item.mappings as Mapping[]) ?? []).map((m) => [
                      m.source_column, { canonical_field: m.canonical_field, transform: m.transform },
                    ])),
                  })}>
            Approve mapping
          </button>
        )}
        {!["BLANK_OWNER", "NO_OPPORTUNITY", "MAPPING"].includes(item.kind) && !(item.kind === "AMBIGUOUS" && item.key === "booking_date") && (
          <button disabled={busy} onClick={() => send({ acknowledged: true })}>
            Approve
          </button>
        )}
      </div>
    </div>
  );
}
