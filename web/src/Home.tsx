import { useEffect, useState, type ReactNode } from "react";
import { api, type NeedsYou, type NeedsYouItem, type Principal, type SourceRows } from "./api";
import { Money, Reply, When, isTraceable, personName, usd } from "./format";
import { Drawer, Simulated, Unavailable, useApi } from "./ui";

// D2 + D5: the CFO's morning. A KPI strip for the period, every input Quota reads (open one to see its rows,
// with the fields Quota extracted marked), and on the right everything waiting on a person, grouped by person.

type Props = { people: Principal[]; version: number; onChange: () => void; source: string | null };

export function HomeView({ people, version, onChange, source }: Props) {
  const home = useApi(() => api.home(), [version]);
  const sources = useApi(() => api.sources(), [version]);

  return (
    <section className="home">
      {home.missing ? (
        <Unavailable what="The KPI strip" path="/api/home" />
      ) : (
        home.data && (
          <div className="kpis">
            <Kpi label={`pending commission${home.data.period ? `, ${home.data.period}` : ""}`}>
              <a className="money" href="#reps" title="By rep">
                {usd(home.data.pending_cents)}
              </a>
            </Kpi>
            <Kpi label="cleared by evidence">{home.data.cleared_pct === null ? "–" : `${home.data.cleared_pct}%`}</Kpi>
            <Kpi label="held" human={home.data.held_cents > 0}
                 note={Object.entries(home.data.held_by_reason).map(([r, n]) => `${n} ${r.replace(/_/g, " ")}`).join(" · ")}>
              <a className="money" href="#home" title="Held lines are in Needs you">
                {usd(home.data.held_cents)}
              </a>
            </Kpi>
            <Kpi label="needs you" human={home.data.needs_you > 0}>
              {home.data.needs_you}
            </Kpi>
          </div>
        )
      )}
      <div className="home-grid">
        <div>
          <h3>Sources</h3>
          {sources.missing ? (
            <Unavailable what="The sources rail" path="/api/sources" />
          ) : (
            <div className="rail">
              {(sources.data ?? []).map((s) => (
                <a key={s.kind} className={`card source-card ${source === s.kind ? "on" : ""}`} href={`#home/source/${encodeURIComponent(s.kind)}`}>
                  <div className="card-head">
                    <strong>{s.label}</strong>
                    {/simulated/i.test(s.via ?? "") && <Simulated />}
                  </div>
                  <div className="source-count">{s.count.toLocaleString("en-US")}</div>
                  <div className="muted">
                    {s.via && <>via {s.via}</>}
                    {s.last_sync && (
                      <>
                        {" "}
                        · synced <When ts={s.last_sync} />
                      </>
                    )}
                  </div>
                  {s.detail && <div className="muted source-detail">{s.detail}</div>}
                </a>
              ))}
            </div>
          )}
        </div>
        <NeedsYouInbox people={people} version={version} onChange={onChange} />
      </div>
      {source && <SourceDrawer kind={source} label={sources.data?.find((s) => s.kind === source)?.label ?? source} />}
    </section>
  );
}

function Kpi({ label, children, note, human }: { label: string; children: ReactNode; note?: string; human?: boolean }) {
  return (
    <div className={`tile kpi ${human ? "human" : ""}`}>
      <div className="tile-value">{children}</div>
      <div className="tile-label">{label}</div>
      {note && <div className="tile-note">{note}</div>}
    </div>
  );
}

/** The rows of one source, as they arrived; extracted fields marked, a contract page with its quoted spans. */
function SourceDrawer({ kind, label }: { kind: string; label: string }) {
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const rows = useApi(() => api.sourceRows(kind, query), [kind, query]);
  useEffect(() => {
    const t = window.setTimeout(() => setQuery(q), 250);
    return () => window.clearTimeout(t);
  }, [q]);

  return (
    <Drawer title={label} back="home" wide>
      <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter rows (Acme, Orbit, a rep…)" aria-label="Filter rows" />
      {rows.missing && <Unavailable what="The raw viewer" path={`/api/sources/${kind}/rows`} />}
      {rows.error && <p className="error">{rows.error}</p>}
      {rows.data && <RawRows data={rows.data} />}
    </Drawer>
  );
}

function RawRows({ data }: { data: SourceRows }) {
  return (
    <>
      <p className="muted">
        {data.total !== undefined && data.total !== data.rows.length ? `${data.rows.length} of ${data.total}` : data.rows.length} row
        {(data.total ?? data.rows.length) === 1 ? "" : "s"} · <mark className="legend">marked</mark> = what Quota extracted or quoted
      </p>
      <div className="table-wrap">
        <table className="table raw-rows">
          <thead>
            <tr>
              {data.columns.map((c) => (
                <th key={c}>{c}</th>
              ))}
              <th>Ref</th>
            </tr>
          </thead>
          <tbody>
            {data.rows.map((row, i) => (
              <tr key={String(row.ref ?? i)}>
                {data.columns.map((c) => {
                  const v = row[c];
                  const text = v === null || v === undefined ? "" : typeof v === "string" ? v : JSON.stringify(v);
                  const whole = row.highlight?.includes(c);
                  // A message's highlight is the quoted span of its text, not a column name.
                  const spans = (row.highlight ?? []).filter((h) => !data.columns.includes(h) && text.includes(h));
                  return (
                    <td key={c} className={whole ? "hl" : ""}>
                      {whole ? <mark>{text}</mark> : spans.length ? <Marked text={text} spans={spans} /> : text}
                    </td>
                  );
                })}
                <td className="mono muted">{row.ref}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

/** Text with the quoted spans marked (an email or Slack message Quota read a commitment from). */
function Marked({ text, spans }: { text: string; spans: string[] }) {
  const parts: ReactNode[] = [];
  let rest = text;
  let key = 0;
  for (const span of spans) {
    const at = rest.indexOf(span);
    if (at < 0) continue;
    parts.push(rest.slice(0, at), <mark key={key++}>{span}</mark>);
    rest = rest.slice(at + span.length);
  }
  parts.push(rest);
  return <>{parts}</>;
}

/** D5: everything waiting on a person, by person, each with its question, dollar impact, evidence and chain. */
export function NeedsYouInbox({ people, version, onChange, only }: { people: Principal[]; version: number; onChange: () => void; only?: string }) {
  const [tick, setTick] = useState(0);
  const inbox = useApi<NeedsYou>(() => api.needsYou(), [version, tick]);
  if (inbox.missing) return <Unavailable what="Needs you" path="/api/needs-you" />;
  const groups = (inbox.data?.people ?? []).filter((g) => !only || g.person === only);
  const total = groups.reduce((n, g) => n + g.items.length, 0);
  return (
    <div className={`needs-you ${total ? "human-panel" : ""}`}>
      <h3>
        Needs you <span className="total">{total}</span>
      </h3>
      {inbox.data && total === 0 && <p className="muted">Nothing is waiting on a person.</p>}
      {groups.map((g) => (
        <div key={g.person} className="ny-person">
          <div className="who-name">
            {g.name ?? personName(g.person, people)}, {g.title}
          </div>
          {g.items.map((item) => (
            <NeedsYouCard key={item.id} item={item} person={g.person} people={people}
                          onAnswered={() => {
                            setTick((t) => t + 1);
                            onChange();
                          }} />
          ))}
        </div>
      ))}
    </div>
  );
}

export function NeedsYouCard({ item, person, people, onAnswered }: { item: NeedsYouItem; person: string; people: Principal[]; onAnswered: () => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const trace = isTraceable(item.id) ? item.id : null;

  async function answer(decision: "approve" | "reject" | "reply") {
    setBusy(true);
    setError(null);
    try {
      const res = await api.answer(item.id, person, decision, text);
      // The write path still decides who may answer; a refusal stays on the card with its reason.
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
    <div className="card human ny-item">
      <div className="card-head">
        <span className="tag">{item.kind.replace(/_/g, " ")}</span>
        {trace && <a className="ref" href={`#trace/${encodeURIComponent(trace)}`}>{trace}</a>}
        <span className="ny-impact">
          <Money cents={item.impact_cents} trace={trace} />
        </span>
      </div>
      <div className="ny-question">
        <Reply text={item.question} trace={trace} />
      </div>
      {(item.deals ?? []).length > 0 && (
        <div className="muted">
          {(item.deals ?? []).map((d) => (
            <a key={d} className="ref" href={`#trace/deal/${encodeURIComponent(d)}`}>
              {d}
            </a>
          ))}
        </div>
      )}
      {item.evidence && item.evidence.length > 0 && (
        <ul className="ny-evidence">
          {item.evidence.map((e, i) => (
            <li key={i}>
              {e.decision && <a className="ref" href={`#trace/${encodeURIComponent(e.decision)}`}>{e.decision}</a>}{" "}
              {e.text ? <Reply text={e.text} trace={e.decision ?? trace} /> : e.ref && <Reply text={e.ref} />}
            </li>
          ))}
        </ul>
      )}
      {item.chain && item.chain.length > 0 && (
        <ol className="ny-chain">
          {item.chain.map((c, i) => (
            <li key={i} className={/refused/i.test(c.text) ? "refused" : ""}>
              <When ts={c.at} /> {/refused/i.test(c.text) && "🔒 "}
              <Reply text={c.text} />
            </li>
          ))}
        </ol>
      )}
      <div className="ny-answer">
        <textarea value={text} onChange={(e) => setText(e.target.value)} rows={1}
                  placeholder={`Answer as ${personName(person, people)} (a reason, or a reply)`} aria-label="Answer" />
        <div className="actions">
          {item.answer !== "reply" && (
            <>
              <button className="primary" disabled={busy} onClick={() => answer("approve")}>
                Approve
              </button>
              <button disabled={busy} onClick={() => answer("reject")}>
                Reject
              </button>
            </>
          )}
          <button className={item.answer === "reply" ? "primary" : ""} disabled={busy || !text.trim()} onClick={() => answer("reply")}>
            Reply
          </button>
        </div>
      </div>
      {error && <p className="error">{error}</p>}
    </div>
  );
}
