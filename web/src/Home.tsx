import { Fragment, useCallback, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { api, isMissing, type NeedsYou, type NeedsYouItem, type Principal, type SourceRows } from "./api";
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

// ---------- D5: Needs you ----------
// Everything waiting on a person, by person. Each card says in one line what is being decided and what it is worth,
// then the seat's own question, why it is here and how it got here, and an answer box. An answered card stays in
// place for the session in its resolved state (green approved, red rejected, blue reply sent) until Dismiss or a
// reload, with the server's result in words and any copies the answer cleared.

type Decision = "approve" | "reject" | "reply";
type Entry = { item: NeedsYouItem; person: string; name?: string; title: string; seq: number };
type Outcome = {
  decision: Decision;
  comment: string;
  by: string;
  result: unknown;
  after: number; // the last inbox fetch started before the answer landed
  cleared: { id: string; name: string }[];
  routed: { id: string; name: string } | null;
};
type Answered = { ok: true } | { ok: false; reason: string; codes: string[] };

type KindWords = { label: string; ask: string; final: string; rejected: string; amount: string };
const KINDS: Record<string, KindWords> = {
  plan_exception: {
    label: "Plan exception",
    ask: "grant the exception?",
    final: "Final: the exception is granted and becomes policy.",
    rejected: "Declined: the line stays as calculated, and the rep is told why.",
    amount: "at stake",
  },
  commitment_review: {
    label: "Off-contract promise review",
    ask: "is the side promise binding?",
    final: "Final: the promise is recorded as not binding, so the held commission can be released.",
    rejected: "Rejected: the promise counts as binding; the commission stays held until the contract is amended.",
    amount: "held",
  },
  commitment_check: {
    label: "Off-contract promise check",
    ask: "is the side promise binding?",
    final: "Final: the promise is recorded as not binding.",
    rejected: "Rejected: the commission stays held.",
    amount: "held",
  },
  journal_entry: {
    label: "Journal entry approval",
    ask: "post this entry?",
    final: "Final: the journal entry is approved and posts to the ledger.",
    rejected: "Rejected: the entry does not post.",
    amount: "entry",
  },
  payroll_file: {
    label: "Payroll file approval",
    ask: "release this payroll file?",
    final: "Final: the payroll file is approved for release.",
    rejected: "Rejected: the payroll file is held back.",
    amount: "payroll",
  },
};

function kindWords(kind: string): KindWords {
  const words = kind.replace(/_/g, " ");
  return (
    KINDS[kind] ?? {
      label: words.charAt(0).toUpperCase() + words.slice(1),
      ask: "approve?",
      final: "Final: approved; no further signature is needed.",
      rejected: "Rejected: nothing changes.",
      amount: "at stake",
    }
  );
}

const CODE_WORDS: Record<string, string> = {
  INV5_SELF_APPROVAL: "cannot approve your own request",
  INV5_WRONG_APPROVER: "not the approver on record",
  ALREADY_DECIDED: "already decided",
  ALREADY_SETTLED: "a backup approver already settled it",
};

const MOD = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl";

const LEAD = /^([A-Z][\w&.'-]*(?: [A-Z][\w&.'-]*){0,4}): /;

/** The customers an item is about: the account name when known, or the seat's own lead-in ("Summit Dental: …").
 * `body` is the question without that lead-in when the headline already says it. */
function customers(item: NeedsYouItem): { names: { deal: string; name: string }[]; body: string } {
  const lead = item.question.match(LEAD)?.[1];
  const names = (item.deals ?? []).map((deal) => {
    const name = accountName(deal);
    return { deal, name: lead && lead.length > name.length && lead.toLowerCase().startsWith(deal.toLowerCase()) ? lead : name };
  });
  const said = lead && names.some((n) => n.name === lead);
  const rest = item.question.replace(LEAD, "");
  return { names, body: said ? rest.charAt(0).toUpperCase() + rest.slice(1) : item.question };
}

function summarize(result: unknown): string {
  if (result === null || result === undefined) return "The server accepted the answer.";
  if (typeof result !== "object") return String(result);
  return Object.entries(result as Record<string, unknown>)
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => `${k.replace(/_/g, " ")}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
    .join(" · ");
}

export function NeedsYouInbox({ people, version, onChange, only }: { people: Principal[]; version: number; onChange: () => void; only?: string }) {
  const [data, setData] = useState<NeedsYou | null>(null);
  const [missing, setMissing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Every item seen this session (so an answered card keeps its place), and the answers given here.
  const [known, setKnown] = useState<Record<string, Entry>>({});
  const [resolved, setResolved] = useState<Record<string, Outcome>>({});
  const [pinned, setPinned] = useState<Record<string, true>>({}); // refused here: kept on screen until Dismiss
  const issued = useRef(0);
  const applied = useRef(0);
  const seq = useRef(0);
  const seen = useRef<Record<string, Entry>>({});
  const latest = useRef<NeedsYou | null>(null);

  const load = useCallback(async (): Promise<NeedsYou | null> => {
    const n = ++issued.current;
    try {
      const d = await api.needsYou();
      if (n < applied.current) return d; // an older fetch finishing late (a poll overlapping an answer)
      applied.current = n;
      latest.current = d;
      setData(d);
      setMissing(false);
      setError(null);
      // Places are assigned here, once, in the server's order (not inside a state updater, which may run twice).
      const next = { ...seen.current };
      for (const g of d.people)
        for (const item of g.items)
          next[item.id] = { item, person: g.person, name: g.name, title: g.title, seq: seen.current[item.id]?.seq ?? seq.current++ };
      seen.current = next;
      setKnown(next);
      // An approval or rejection the server shows open again (a demo reset, a CFO Reset rerun) is open again.
      // A reply leaves its item open by design, so a reply card stays until Dismiss.
      setResolved((prev) => {
        const open = new Set(d.people.flatMap((g) => g.items.map((i) => i.id)));
        const stale = Object.keys(prev).filter((id) => prev[id].decision !== "reply" && open.has(id) && n > prev[id].after);
        if (!stale.length) return prev;
        const next = { ...prev };
        stale.forEach((id) => delete next[id]);
        return next;
      });
      return d;
    } catch (e) {
      if (n >= applied.current) {
        if (isMissing(e)) setMissing(true);
        else setError((e as Error).message);
      }
      return null;
    }
  }, []);

  useEffect(() => {
    void load();
  }, [version, load]);

  const nameOf = (g: { person: string; name?: string }) => g.name ?? personName(g.person, people);

  async function answer(entry: Entry, decision: Decision, text: string): Promise<Answered> {
    const { item, person } = entry;
    const before = latest.current;
    let res: Awaited<ReturnType<typeof api.answer>>;
    try {
      res = await api.answer(item.id, person, decision, text);
    } catch (e) {
      return { ok: false, reason: (e as Error).message, codes: [] };
    }
    const result = res.result;
    if (!res.ok) {
      // The write path still decides who may answer. Its refusal lands in the item's chain; show the new codes.
      // The card stays on screen with the refusal even if the item has left the inbox (settled by a backup).
      setPinned((prev) => ({ ...prev, [item.id]: true }));
      const reason = typeof res.refused === "string" ? res.refused : res.refused ? JSON.stringify(res.refused) : "The write path refused it.";
      const fresh = await load();
      const seen = new Set((item.chain ?? []).map((c) => c.at + c.text));
      const now = fresh?.people.flatMap((g) => g.items).find((i) => i.id === item.id);
      const codes = (now?.chain ?? [])
        .filter((c) => !seen.has(c.at + c.text))
        .flatMap((c) => c.text.match(/refused: (.+)$/)?.[1].split(/,\s*/) ?? []);
      return { ok: false, reason, codes };
    }
    const by = personName(person, people);
    setResolved((prev) => ({ ...prev, [item.id]: { decision, comment: text, by, result, after: issued.current, cleared: [], routed: null } }));
    const fresh = await load();
    if (fresh) {
      const open = new Map(fresh.people.flatMap((g) => g.items.map((i) => [i.id, g] as const)));
      const cleared = (before?.people ?? []).flatMap((g) =>
        g.items.filter((i) => i.id !== item.id && !open.has(i.id)).map((i) => ({ id: i.id, name: nameOf(g) })),
      );
      const nextId = (result as { next_approval_id?: string | null } | null | undefined)?.next_approval_id ?? null;
      const nextGroup = nextId ? open.get(nextId) : undefined;
      const routed = nextId ? { id: nextId, name: nextGroup ? nameOf(nextGroup) : "the next approver" } : null;
      setResolved((prev) =>
        prev[item.id] ? { ...prev, [item.id]: { ...prev[item.id], cleared: cleared.filter((c) => !prev[c.id]), routed } } : prev,
      );
    }
    onChange();
    return { ok: true };
  }

  function dismiss(id: string) {
    const without = <T,>(prev: Record<string, T>) => {
      const next = { ...prev };
      delete next[id];
      return next;
    };
    setResolved(without);
    setPinned(without);
  }

  if (missing) return <Unavailable what="Needs you" path="/api/needs-you" />;

  const openIds = new Set((data?.people ?? []).flatMap((g) => g.items.map((i) => i.id)));
  const entries = Object.values(known)
    .filter((e) => (!only || e.person === only) && (openIds.has(e.item.id) || resolved[e.item.id] || pinned[e.item.id]))
    .sort((a, b) => a.seq - b.seq);
  const persons = [...new Set([...(data?.people ?? []).map((g) => g.person), ...entries.map((e) => e.person)])].filter((p) =>
    entries.some((e) => e.person === p),
  );
  // Waiting is what the server still shows open: a reply does not settle an item, so a replied card still counts.
  const waiting = (e: Entry) => openIds.has(e.item.id);
  const open = entries.filter(waiting).length;
  // A copy an answer cleared (a backup's A-0003 when A-0002 is approved) counts as answered too.
  const cleared = entries.reduce((n, e) => n + (resolved[e.item.id]?.cleared.filter((c) => !openIds.has(c.id)).length ?? 0), 0);
  const answered = entries.filter((e) => resolved[e.item.id] && !waiting(e)).length + cleared;
  const total = open + answered;

  return (
    <div className={`needs-you nyx ${open ? "human-panel" : data ? "nyx-done" : ""}`}>
      <div className="nyx-header">
        <h3>
          Needs you <span className="nyx-count" aria-label={`${open} open`}>{open}</span>
        </h3>
        {answered > 0 && (
          <span className="nyx-progress" aria-live="polite">
            <span className="nyx-bar" aria-hidden="true">
              <span style={{ width: `${Math.round((100 * answered) / total)}%` }} />
            </span>
            {answered} of {total} answered
          </span>
        )}
      </div>
      {open > 0 && <p className="nyx-sub">Decisions only a person can make. Every id and amount opens its trace.</p>}
      {error && !data && <p className="error">{error}</p>}
      {data && open === 0 && (
        <div className="nyx-clear" role="status">
          <span className="nyx-mark" aria-hidden="true">✓</span>
          All clear: nothing is waiting on a person.
        </div>
      )}
      {persons.map((p) => {
        const mine = entries.filter((e) => e.person === p);
        const left = mine.filter(waiting).length;
        const head = mine[0];
        return (
          <div key={p} className="nyx-person">
            <div className="nyx-person-head">
              <span className="who-name">{head.name ?? personName(p, people)}</span>
              {head.title && <span className="muted">{head.title}</span>}
              <span className="nyx-person-left">{left ? `${left} waiting` : "done"}</span>
            </div>
            {mine.map((e) => (
              <NeedsYouCard
                key={e.item.id}
                item={e.item}
                person={e.person}
                people={people}
                outcome={resolved[e.item.id]}
                stillOpen={openIds.has(e.item.id)}
                onAnswer={(decision, text) => answer(e, decision, text)}
                onDismiss={() => dismiss(e.item.id)}
              />
            ))}
          </div>
        );
      })}
    </div>
  );
}

export function NeedsYouCard({
  item,
  person,
  people,
  outcome,
  stillOpen,
  onAnswer,
  onDismiss,
}: {
  item: NeedsYouItem;
  person: string;
  people: Principal[];
  outcome?: Outcome;
  stillOpen?: boolean;
  onAnswer: (decision: Decision, text: string) => Promise<Answered>;
  onDismiss?: () => void;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState<Decision | null>(null);
  const [hint, setHint] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<{ reason: string; codes: string[] } | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [why, setWhy] = useState(false);
  const [how, setHow] = useState(false);
  const box = useRef<HTMLTextAreaElement>(null);

  const trace = isTraceable(item.id) ? item.id : null;
  const words = kindWords(item.kind);
  const replyOnly = item.answer === "reply";
  const { names, body } = customers(item);
  const who = personName(person, people);
  const evidence = item.evidence ?? [];
  const chain = item.chain ?? [];
  const refusedSteps = chain.filter((c) => /refused/i.test(c.text)).length;
  const long = body.length > 200;
  const state = outcome
    ? { approve: "is-approved", reject: "is-rejected", reply: "is-replied" }[outcome.decision]
    : refusal
      ? "is-refused"
      : "is-open";

  async function submit(decision: Decision) {
    const note = text.trim();
    if (decision !== "approve" && !note) {
      setHint(decision === "reject" ? "Add a reason to reject: the person who asked will read it." : "Write the reply first.");
      box.current?.focus();
      return;
    }
    setBusy(decision);
    setHint(null);
    setRefusal(null);
    const out = await onAnswer(decision, note);
    setBusy(null);
    if (out.ok) setText("");
    else setRefusal({ reason: out.reason, codes: out.codes });
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      if (!busy) void submit(replyOnly ? "reply" : "approve");
    }
  }

  return (
    <article className={`card nyx-card ${state}`} aria-busy={busy ? true : undefined}>
      <header className="nyx-head">
        <div className="nyx-head-main">
          <div className="nyx-eyebrow">
            <span className="nyx-kind">{words.label}</span>
            {trace && <TraceLink id={trace} />}
          </div>
          <div className="nyx-title">
            {names.length ? (
              <>
                {names.map((n, i) => (
                  <Fragment key={n.deal}>
                    {i > 0 && ", "}
                    <a className="nyx-deal" href={`#trace/deal/${encodeURIComponent(n.deal)}`} title={`Trace deal ${n.deal}`}>
                      {n.name}
                    </a>
                  </Fragment>
                ))}
                <span className="nyx-ask">: {words.ask}</span>
              </>
            ) : (
              <span className="nyx-ask">{words.ask.charAt(0).toUpperCase() + words.ask.slice(1)}</span>
            )}
          </div>
        </div>
        {item.impact_cents !== null && item.impact_cents !== undefined && (
          <div className="nyx-amount">
            <Money cents={item.impact_cents} trace={trace} />
            <span className="nyx-amount-label">{words.amount}</span>
          </div>
        )}
      </header>

      {outcome ? (
        <Resolved item={item} trace={trace} words={words} outcome={outcome} stillOpen={!!stillOpen} onDismiss={onDismiss} />
      ) : (
        <>
          <div className={`nyx-question ${long && !expanded ? "clamped" : ""}`}>
            {expanded ? <Reply text={body} trace={trace} /> : <Rich text={body} trace={trace} />}
          </div>
          {long && (
            <button type="button" className="nyx-link nyx-more" aria-expanded={expanded} onClick={() => setExpanded((v) => !v)}>
              {expanded ? "Show less" : "Show more"}
            </button>
          )}

          {(evidence.length > 0 || chain.length > 0) && (
            <div className="nyx-folds">
              {evidence.length > 0 && (
                <button type="button" className="nyx-fold" aria-expanded={why} onClick={() => setWhy((v) => !v)}>
                  <span className="nyx-caret" aria-hidden="true">▶</span>
                  Why it's here
                  <span className="nyx-fold-n">{evidence.length}</span>
                </button>
              )}
              {chain.length > 0 && (
                <button type="button" className="nyx-fold" aria-expanded={how} onClick={() => setHow((v) => !v)}>
                  <span className="nyx-caret" aria-hidden="true">▶</span>
                  How it got here
                  <span className={`nyx-fold-n ${refusedSteps ? "bad" : ""}`}>
                    {chain.length} step{chain.length === 1 ? "" : "s"}
                    {refusedSteps > 0 && ` · 🔒 ${refusedSteps}`}
                  </span>
                </button>
              )}
            </div>
          )}
          {why && evidence.length > 0 && (
            <div className="nyx-pane">
              <div className="nyx-pane-label">Why it's here</div>
              <ul className="nyx-why">
                {evidence.map((e, i) => (
                  <li key={i}>
                    {e.decision && <TraceLink id={e.decision} />}{" "}
                    {e.text ? <Reply text={e.text} trace={e.decision ?? trace} /> : e.ref && <Reply text={e.ref} />}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {how && chain.length > 0 && (
            <div className="nyx-pane">
              <div className="nyx-pane-label">How it got here</div>
              <ol className="nyx-chain">
                {chain.map((c, i) => (
                  <li key={i} className={/refused/i.test(c.text) ? "refused" : /waiting/i.test(c.text) ? "waiting" : ""}>
                    <When ts={c.at} />
                    <Reply text={c.text} />
                  </li>
                ))}
              </ol>
            </div>
          )}

          {refusal && (
            <div className="nyx-refusal" role="alert">
              <strong>🔒 Refused.</strong> {refusal.reason}
              {refusal.codes.length > 0 && (
                <div className="nyx-codes">
                  {refusal.codes.map((c) => (
                    <span key={c} className="nyx-code">
                      {c}
                      {CODE_WORDS[c] && <em>{CODE_WORDS[c]}</em>}
                    </span>
                  ))}
                </div>
              )}
              {stillOpen === false && (
                <div className="nyx-gone">
                  It is no longer open: someone else settled it first.
                  {onDismiss && (
                    <button type="button" className="nyx-link nyx-dismiss" onClick={onDismiss}>
                      Dismiss
                    </button>
                  )}
                </div>
              )}
            </div>
          )}

          {stillOpen !== false && <div className="nyx-answer">
            <label htmlFor={`nyx-note-${item.id}`}>
              {replyOnly ? `Reply as ${who}` : `Note from ${who}`}
              <span className="muted"> {replyOnly ? "(required)" : "(optional to approve, required to reject or reply)"}</span>
            </label>
            <textarea
              id={`nyx-note-${item.id}`}
              ref={box}
              className={`${hint ? "needs-text" : ""} ${text ? "has-text" : ""}`}
              value={text}
              rows={1}
              onChange={(e) => {
                setText(e.target.value);
                if (hint && e.target.value.trim()) setHint(null);
              }}
              onKeyDown={onKey}
              placeholder={replyOnly ? "Your answer to the analyst" : "Why you decided this (the person who asked sees it)"}
            />
            <div className="nyx-actions">
              {!replyOnly && (
                <>
                  <button type="button" className="nyx-approve" disabled={!!busy} onClick={() => void submit("approve")}>
                    {busy === "approve" ? "Approving…" : "✓ Approve"}
                  </button>
                  <button type="button" className="nyx-reject" disabled={!!busy} onClick={() => void submit("reject")}>
                    {busy === "reject" ? "Rejecting…" : "Reject"}
                  </button>
                </>
              )}
              <button type="button" className={`nyx-reply ${replyOnly ? "primary" : ""}`} disabled={!!busy} onClick={() => void submit("reply")}>
                {busy === "reply" ? "Sending…" : "Reply"}
              </button>
              <span className="nyx-kbd">
                <kbd>{MOD}</kbd>+<kbd>Enter</kbd> {replyOnly ? "sends" : "approves"}
              </span>
            </div>
            {hint && (
              <p className="nyx-hint" role="alert">
                {hint}
              </p>
            )}
          </div>}
        </>
      )}
    </article>
  );
}

/** An answered card: who decided, their note, what the server did with it, and the copies it cleared. */
function Resolved({
  item,
  trace,
  words,
  outcome,
  stillOpen,
  onDismiss,
}: {
  item: NeedsYouItem;
  trace: string | null;
  words: KindWords;
  outcome: Outcome;
  stillOpen: boolean;
  onDismiss?: () => void;
}) {
  // A reply leaves the item open: say so, and offer the answer box again instead of a plain Dismiss.
  const pending = outcome.decision === "reply" && stillOpen;
  const r = outcome.result as { result?: string; next_approval_id?: string | null } | null | undefined;
  const [mark, verdict] =
    outcome.decision === "approve"
      ? ["✓", `Approved by ${outcome.by}`]
      : outcome.decision === "reject"
        ? ["✕", `Rejected by ${outcome.by}`]
        : ["↗", "Reply sent"];
  let line: ReactNode;
  let raw = false;
  if (outcome.decision === "reply") line = "Sent to Quota's commissions analyst; its answer lands on the timeline.";
  else if (r?.result === "final") line = words.final;
  else if (r?.result === "rejected") line = words.rejected;
  else if (r?.result === "over_limit_routed") {
    const next = outcome.routed?.id ?? r.next_approval_id ?? null;
    line = (
      <>
        Over {outcome.by}'s approval limit: routed to {outcome.routed?.name ?? "the next approver"}
        {next && (
          <>
            {" "}
            as <TraceLink id={next} />
          </>
        )}{" "}
        for the next signature.
      </>
    );
  } else {
    line = summarize(outcome.result);
    raw = true;
  }

  return (
    <div className="nyx-resolved">
      <div className="nyx-verdict" role="status">
        <span className="nyx-mark" aria-hidden="true">
          {mark}
        </span>
        <strong>{verdict}</strong>
        {onDismiss && (
          <button type="button" className="nyx-link nyx-dismiss" onClick={onDismiss}>
            {pending ? "Answer now" : "Dismiss"}
          </button>
        )}
      </div>
      {outcome.comment && (
        <blockquote className="nyx-comment">
          “{outcome.comment}”<cite>{outcome.by}</cite>
        </blockquote>
      )}
      <p className={`nyx-result ${raw ? "raw" : ""}`}>{line}</p>
      {pending && <p className="nyx-cleared">Still open: {item.answer === "reply" ? "it clears on the next check" : `it waits on a decision from ${outcome.by}`}.</p>}
      {outcome.cleared.length > 0 && (
        <p className="nyx-cleared">
          Also cleared:{" "}
          {outcome.cleared.map((c, i) => (
            <Fragment key={c.id}>
              {i > 0 && ", "}
              <TraceLink id={c.id} /> ({c.name})
            </Fragment>
          ))}
        </p>
      )}
      <details className="nyx-asked">
        <summary>What was asked</summary>
        <div className="nyx-question">
          <Reply text={item.question} trace={trace} />
        </div>
      </details>
    </div>
  );
}
