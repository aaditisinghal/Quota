import { Fragment, useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type ReactNode } from "react";
import { api, type NeedsYou, type NeedsYouItem, type Principal, type SourceRows } from "./api";
import { Money, Reply, Rich, TraceLink, When, accountName, isTraceable, personName, usd } from "./format";
import { Drawer, Simulated, Unavailable, useApi } from "./ui";
import "./needsyou.css";

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

/* ---------- D5: Needs you ---------- */

// Everything waiting on a person, by person. Each card says what kind of question it is, which customer, and how
// much money rides on it; the seat's own words; why it is here and how it got here. Answering keeps the card in
// place, turned green (approved), red (rejected) or blue (reply sent), with the comment and what happened next.

type Decision = "approve" | "reject" | "reply";
type Entry = { item: NeedsYouItem; person: string; name: string; title: string };
type AnswerResult = { result?: string; next_approval_id?: string | null; replied?: boolean } | string | null | undefined;
type Resolution = {
  entry: Entry;
  groupIndex: number;
  itemIndex: number;
  decision: Decision;
  text: string;
  result: AnswerResult;
  alsoCleared: { id: string; name: string; person: string }[];
  /** The item was gone from the inbox right after the answer; if it comes back (a demo reset), it is open again. */
  goneAfter: boolean;
  at: number;
  dismissed?: boolean;
};

const KIND_LABEL: Record<string, string> = {
  plan_exception: "Plan exception",
  commitment_review: "Off-contract promise",
  commitment_check: "Off-contract promise",
  termination_review: "Termination review",
  manager_override: "Manager override",
  clawback: "Clawback",
  dispute: "Rep dispute",
  true_up: "True-up",
  escalation: "Escalation",
  collection_hold: "Collection hold",
  splits_check: "Split check",
  territory_check: "Territory check",
  backdating_check: "Backdating check",
  authority_check: "Approval authority",
  spiff_claim: "SPIFF claim",
  plan_change_proposal: "Plan change",
  amendment: "Contract amendment",
  payment: "Payout",
  payouts: "Payout",
  void_reversal: "Reversal",
};

export function kindLabel(kind: string): string {
  if (KIND_LABEL[kind]) return KIND_LABEL[kind];
  const words = kind.replace(/_/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

const REFUSED = /refused/i;

function refusalText(res: { refused?: unknown; result?: unknown }): string {
  if (typeof res.refused === "string") return res.refused.charAt(0).toUpperCase() + res.refused.slice(1);
  if (res.refused) return `Refused: ${JSON.stringify(res.refused)}`;
  return `Refused: ${JSON.stringify(res.result ?? "no reason given")}`;
}

function allItems(data: NeedsYou | null): Entry[] {
  return (data?.people ?? []).flatMap((g) =>
    g.items.map((item) => ({ item, person: g.person, name: g.name ?? g.person, title: g.title })),
  );
}

export function NeedsYouInbox({ people, version, onChange, only }: { people: Principal[]; version: number; onChange: () => void; only?: string }) {
  const [tick, setTick] = useState(0);
  const inbox = useApi<NeedsYou>(() => api.needsYou(), [version, tick]);
  const [data, setData] = useState<NeedsYou | null>(null);
  const [resolved, setResolved] = useState<Record<string, Resolution>>({});
  const latest = useRef<NeedsYou | null>(null);
  latest.current = data;

  useEffect(() => {
    if (inbox.data) setData(inbox.data);
  }, [inbox.data]);

  // A card answered this session that the server shows again (after a reset) is open again, not resolved.
  useEffect(() => {
    if (!data) return;
    const open = new Set(allItems(data).map((e) => e.item.id));
    const stale = Object.values(resolved).filter((r) => r.goneAfter && open.has(r.entry.item.id));
    if (stale.length) {
      setResolved((all) => {
        const next = { ...all };
        for (const r of stale) delete next[r.entry.item.id];
        return next;
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  async function answer(entry: Entry, groupIndex: number, itemIndex: number, decision: Decision, text: string): Promise<string | null> {
    const before = allItems(latest.current);
    let res: Awaited<ReturnType<typeof api.answer>>;
    try {
      res = await api.answer(entry.item.id, entry.person, decision, text);
    } catch (e) {
      return (e as Error).message;
    }
    // The write path still decides who may answer; a refusal stays on the card with its reason.
    if (!res.ok) return refusalText(res);
    let after: NeedsYou | null = null;
    try {
      after = await api.needsYou();
    } catch {
      // the next refetch will catch up
    }
    const afterIds = new Set(allItems(after).map((e) => e.item.id));
    // Only copies of the same question count as cleared by this answer (same deal, or one names the other);
    // anything else that closed in the same settle is the server's business, not this card's.
    const deals = new Set(entry.item.deals ?? []);
    const related = (e: Entry) =>
      (e.item.deals ?? []).some((d) => deals.has(d)) ||
      e.item.question.includes(entry.item.id) ||
      entry.item.question.includes(e.item.id) ||
      (e.item.chain ?? []).some((c) => c.text.includes(entry.item.id));
    const alsoCleared = after
      ? before
          .filter((e) => e.item.id !== entry.item.id && !afterIds.has(e.item.id) && related(e))
          .map((e) => ({ id: e.item.id, name: e.name, person: e.person }))
      : [];
    setResolved((all) => ({
      ...all,
      [entry.item.id]: {
        entry, groupIndex, itemIndex, decision, text, alsoCleared,
        result: res.result as AnswerResult,
        goneAfter: after ? !afterIds.has(entry.item.id) : false,
        at: Date.now(),
      },
    }));
    if (after) setData(after);
    setTick((t) => t + 1);
    onChange();
    return null;
  }

  function forget(id: string) {
    setResolved((all) => {
      const next = { ...all };
      delete next[id];
      return next;
    });
  }

  function dismiss(id: string) {
    setResolved((all) => (all[id] ? { ...all, [id]: { ...all[id], dismissed: true } } : all));
  }

  if (inbox.missing) return <Unavailable what="Needs you" path="/api/needs-you" />;

  // Open items as the server has them, with this session's resolved cards kept where they were.
  type Row = { entry: Entry; res?: Resolution };
  type Group = { person: string; name: string; title: string; rows: Row[] };
  const groups: Group[] = (data?.people ?? [])
    .filter((g) => !only || g.person === only)
    .map((g) => ({
      person: g.person,
      name: g.name ?? personName(g.person, people),
      title: g.title,
      rows: g.items.map((item) => ({ entry: { item, person: g.person, name: g.name ?? g.person, title: g.title }, res: resolved[item.id] })),
    }));
  const present = new Set(groups.flatMap((g) => g.rows.map((r) => r.entry.item.id)));
  const kept = Object.values(resolved)
    .filter((r) => !present.has(r.entry.item.id) && (!only || r.entry.person === only))
    .sort((a, b) => a.groupIndex - b.groupIndex || a.itemIndex - b.itemIndex);
  for (const r of kept) {
    let g = groups.find((x) => x.person === r.entry.person);
    if (!g) {
      g = { person: r.entry.person, name: r.entry.name, title: r.entry.title, rows: [] };
      groups.splice(Math.min(r.groupIndex, groups.length), 0, g);
    }
    g.rows.splice(Math.min(r.itemIndex, g.rows.length), 0, { entry: r.entry, res: r });
  }

  // Answered this session: the cards answered plus the copies an answer cleared (A-0003 when A-0002 is approved).
  const done = new Set<string>();
  for (const r of Object.values(resolved)) {
    if (!only || r.entry.person === only) done.add(r.entry.item.id);
    for (const c of r.alsoCleared) if (!only || c.person === only) done.add(c.id);
  }
  const answered = done.size;
  const open = groups.reduce((n, g) => n + g.rows.filter((r) => !r.res).length, 0);
  const total = open + answered;
  const visible = groups
    .map((g) => ({ ...g, rows: g.rows.filter((r) => !r.res?.dismissed) }))
    .filter((g) => g.rows.length > 0);

  return (
    <section className={`nyb ${open ? "has-open" : "clear"}`} aria-labelledby="nyb-title">
      <header className="nyb-head">
        <h3 id="nyb-title">
          Needs you
          <span className={`nyb-count ${open ? "" : "zero"}`} aria-label={`${open} open`}>{open}</span>
        </h3>
        {total > 0 && answered > 0 && (
          <div className="nyb-progress" aria-label={`${answered} of ${total} answered`}>
            <span>
              {answered} of {total} answered
            </span>
            <span className="nyb-bar" aria-hidden="true">
              <span style={{ width: `${Math.round((100 * answered) / total)}%` }} />
            </span>
          </div>
        )}
      </header>
      {!data && !inbox.error && <p className="muted">Loading…</p>}
      {inbox.error && !data && <p className="error">{inbox.error}</p>}
      {data && open === 0 && (
        <div className="nyb-clear" role="status">
          <span className="nyb-clear-mark" aria-hidden="true">✓</span>
          <span>All clear: nothing is waiting on a person</span>
        </div>
      )}
      {visible.map((g, gi) => {
        const groupOpen = g.rows.filter((r) => !r.res).length;
        return (
          <div key={g.person} className="nyb-person">
            <div className="nyb-who">
              <strong>{g.name}</strong>
              {g.title && <span className="muted">{g.title}</span>}
              <span className="nyb-who-count muted">{groupOpen ? `${groupOpen} open` : "done"}</span>
            </div>
            {g.rows.map((r, ii) => (
              <NeedsYouCard
                key={r.entry.item.id}
                item={r.entry.item}
                person={g.person}
                people={people}
                resolution={r.res}
                onAnswer={(decision, text) => answer(r.entry, gi, ii, decision, text)}
                onDismiss={() => dismiss(r.entry.item.id)}
                onReopen={() => forget(r.entry.item.id)}
                stillOpen={present.has(r.entry.item.id)}
              />
            ))}
          </div>
        );
      })}
    </section>
  );
}

const IS_MAC = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

type CardProps = {
  item: NeedsYouItem;
  person: string;
  people: Principal[];
  resolution?: Resolution;
  stillOpen?: boolean;
  onAnswer: (decision: Decision, text: string) => Promise<string | null>;
  onDismiss?: () => void;
  onReopen?: () => void;
};

export function NeedsYouCard({ item, person, people, resolution, stillOpen, onAnswer, onDismiss, onReopen }: CardProps) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState<Decision | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hint, setHint] = useState<string | null>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const trace = isTraceable(item.id) ? item.id : null;
  const who = personName(person, people);
  const replyOnly = item.answer === "reply";
  const state = resolution ? (resolution.decision === "approve" ? "approved" : resolution.decision === "reject" ? "rejected" : "replied") : "open";

  async function answer(decision: Decision) {
    if (busy) return;
    const comment = text.trim();
    if (decision !== "approve" && !comment) {
      setHint(decision === "reject" ? "Add a reason to reject." : "Write the reply first.");
      box.current?.focus();
      return;
    }
    setBusy(decision);
    setError(null);
    setHint(null);
    const refused = await onAnswer(decision, comment);
    setBusy(null);
    if (refused) setError(refused);
    else setText("");
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      void answer(replyOnly ? "reply" : "approve");
    }
  }

  return (
    <article className={`nyc ${state}`} aria-label={`${kindLabel(item.kind)} ${item.id}`}>
      <div className="nyc-head">
        <div className="nyc-title">
          <span className="nyc-kind">{kindLabel(item.kind)}</span>
          <span className="nyc-sub">
            {(item.deals ?? []).map((d, i) => (
              <Fragment key={d}>
                {i > 0 && ", "}
                <a className="nyc-deal" href={`#trace/deal/${encodeURIComponent(d)}`} title={`Trace deal ${d}`}>
                  {accountName(d)}
                </a>
              </Fragment>
            ))}
            {(item.deals ?? []).length > 0 && <span className="nyc-dot" aria-hidden="true">·</span>}
            {trace ? <TraceLink id={trace} /> : <span className="mono">{item.id}</span>}
          </span>
        </div>
        <div className="nyc-impact">
          {item.impact_cents === null || item.impact_cents === undefined ? (
            <span className="nyc-noimpact">no amount</span>
          ) : (
            <Money cents={item.impact_cents} trace={trace} />
          )}
        </div>
      </div>

      {resolution && <Outcome resolution={resolution} who={resolution.entry.name || who} stillOpen={!!stillOpen} onDismiss={onDismiss} onReopen={onReopen} />}

      <Question text={item.question} trace={trace} collapsed={!!resolution} />

      {item.evidence && item.evidence.length > 0 && (
        <details className="nyc-more">
          <summary>
            Why it's here <span className="nyc-n">{item.evidence.length}</span>
          </summary>
          <ul className="nyc-evidence">
            {item.evidence.map((e, i) => (
              <li key={i}>
                {e.decision && <TraceLink id={e.decision} />}
                {e.text ? (
                  <div className="nyc-ev-text">
                    <Reply text={e.text} trace={e.decision ?? trace} />
                  </div>
                ) : (
                  e.ref && (isTraceable(e.ref) ? <><TraceLink id={e.ref} /> <span className="muted">the record this question is about</span></> : <Reply text={e.ref} />)
                )}
              </li>
            ))}
          </ul>
        </details>
      )}

      {item.chain && item.chain.length > 0 && (
        <details className="nyc-more">
          <summary>
            How it got here <span className="nyc-n">{item.chain.length}</span>
          </summary>
          <ol className="nyc-chain">
            {item.chain.map((c, i) => {
              const refused = REFUSED.test(c.text);
              const current = !resolution && !refused && c.text.startsWith(item.id) && /waiting/.test(c.text);
              return (
                <li key={i} className={refused ? "refused" : current ? "current" : ""}>
                  <span className="nyc-chain-dot" aria-hidden="true">{refused ? "🔒" : ""}</span>
                  <div>
                    <When ts={c.at} className="nyc-chain-at" />
                    <div className="nyc-chain-text">
                      {refused && <span className="sr-only">Refused: </span>}
                      <Reply text={c.text} trace={trace} />
                    </div>
                  </div>
                </li>
              );
            })}
            {resolution && (
              <li className={`outcome ${state}`}>
                <span className="nyc-chain-dot" aria-hidden="true" />
                <div>
                  <span className="nyc-chain-at">Just now</span>
                  <div className="nyc-chain-text">
                    {trace ? <TraceLink id={trace} /> : item.id} {resolution.decision === "approve" ? "approved" : resolution.decision === "reject" ? "rejected" : "replied to"} by{" "}
                    {resolution.entry.name || who}
                  </div>
                </div>
              </li>
            )}
          </ol>
        </details>
      )}

      {!resolution && (
        <div className="nyc-answer">
          <textarea
            ref={box}
            value={text}
            rows={2}
            onChange={(e) => {
              setText(e.target.value);
              if (hint) setHint(null);
            }}
            onKeyDown={onKey}
            placeholder={replyOnly ? `Reply as ${who}` : `Comment as ${who}: optional to approve, needed to reject or reply`}
            aria-label={`Comment on ${item.id} as ${who}`}
            aria-invalid={!!hint}
            disabled={!!busy}
          />
          <div className="nyc-actions">
            {!replyOnly && (
              <>
                <button className="nyc-approve" disabled={!!busy} onClick={() => answer("approve")}>
                  {busy === "approve" ? "Approving…" : "Approve"}
                </button>
                <button className="nyc-reject" disabled={!!busy} onClick={() => answer("reject")}>
                  {busy === "reject" ? "Rejecting…" : "Reject"}
                </button>
              </>
            )}
            <button className={replyOnly ? "nyc-approve" : "nyc-reply"} disabled={!!busy} onClick={() => answer("reply")}>
              {busy === "reply" ? "Sending…" : "Reply"}
            </button>
            <span className="nyc-kbd muted">
              <kbd>{IS_MAC ? "⌘" : "Ctrl"}</kbd>+<kbd>Enter</kbd> {replyOnly ? "replies" : "approves"}
            </span>
          </div>
          {hint && <p className="nyc-hint">{hint}</p>}
          {error && (
            <p className="nyc-refused" role="alert">
              <strong>Not accepted.</strong> <Rich text={error} trace={trace} />
            </p>
          )}
        </div>
      )}
    </article>
  );
}

/** The seat's question, three lines until asked for more. */
function Question({ text, trace, collapsed }: { text: string; trace: string | null; collapsed: boolean }) {
  const [open, setOpen] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const lines = collapsed ? 1 : 3;

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || open) return;
    const measure = () => setOverflows(el.scrollHeight > el.clientHeight + 1);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [text, open, lines]);

  return (
    <div className="nyc-q">
      <div ref={ref} className={`nyc-q-text ${open ? "" : "clamped"}`} style={open ? undefined : ({ WebkitLineClamp: lines } as CSSProperties)}>
        {open ? <Reply text={text} trace={trace} /> : <Rich text={text} trace={trace} />}
      </div>
      {(overflows || open) && (
        <button className="nyc-toggle" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          {open ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
}

/** What happened when the person answered, in plain words. */
function Outcome({ resolution, who, stillOpen, onDismiss, onReopen }: { resolution: Resolution; who: string; stillOpen: boolean; onDismiss?: () => void; onReopen?: () => void }) {
  const { decision, text, result, alsoCleared, entry } = resolution;
  const r = typeof result === "object" && result ? result : null;
  const trace = isTraceable(entry.item.id) ? entry.item.id : null;

  let headline: ReactNode;
  let detail: ReactNode = null;
  if (decision === "approve") {
    headline = <>✓ Approved by {who}</>;
    if (r?.result === "final") detail = <>Final: granted, saved as policy</>;
    else if (r?.result === "over_limit_routed")
      detail = r.next_approval_id ? <>Over the limit: routed on to <TraceLink id={r.next_approval_id} /></> : <>Over the limit: routed on to the next approver</>;
    else detail = summarize(result);
  } else if (decision === "reject") {
    headline = <>✕ Rejected by {who}</>;
    detail = r?.result === "rejected" ? <>Closed: not granted; the line stays as it was</> : summarize(result);
  } else {
    headline = <>Reply sent</>;
    detail = <>{who} wrote to Quota's analyst{stillOpen ? "; the question stays open until it is approved or rejected" : ""}</>;
  }

  return (
    <div className="nyc-outcome" role="status">
      <div className="nyc-outcome-head">
        <strong>{headline}</strong>
        <span className="nyc-outcome-actions">
          {decision === "reply" && stillOpen && onReopen && (
            <button className="nyc-link" onClick={onReopen}>Answer again</button>
          )}
          {onDismiss && (
            <button className="nyc-link" onClick={onDismiss} aria-label={`Dismiss ${entry.item.id}`}>Dismiss</button>
          )}
        </span>
      </div>
      {text && (
        <blockquote className="nyc-comment">
          <Rich text={text} trace={trace} />
        </blockquote>
      )}
      {detail && <div className="nyc-result">{detail}</div>}
      {alsoCleared.length > 0 && (
        <div className="nyc-also">
          Also cleared:{" "}
          {alsoCleared.map((c, i) => (
            <Fragment key={c.id}>
              {i > 0 && ", "}
              <TraceLink id={c.id} /> ({c.name})
            </Fragment>
          ))}
        </div>
      )}
    </div>
  );
}

function summarize(result: AnswerResult): ReactNode {
  if (!result) return "Recorded";
  if (typeof result === "string") return <Rich text={result} />;
  const parts: ReactNode[] = [];
  if (result.result) parts.push(result.result.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()));
  if (result.next_approval_id) parts.push(<>next: <TraceLink id={result.next_approval_id} /></>);
  if (!parts.length) return "Recorded";
  return (
    <>
      {parts.map((p, i) => (
        <Fragment key={i}>
          {i > 0 && "; "}
          {p}
        </Fragment>
      ))}
    </>
  );
}
