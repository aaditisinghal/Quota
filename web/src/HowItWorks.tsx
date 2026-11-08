import { useEffect, useState, type ReactNode } from "react";
import { api, type Architecture } from "./api";
import { When, usd } from "./format";
import "./how.css";

// The backend, layer by layer, as the code runs it. Every box opens to what it collects, what it does, where it
// shows up on screen and where it lives in the code; the numbers are live counts from the running world.

type Status = "real" | "simulated" | "built" | "in progress";
type Box = {
  title: string;
  line: string;
  status?: Status;
  stat?: (a: Architecture) => ReactNode;
  collects?: string[];
  does?: string[];
  shown?: [string, string][]; // [label, hash]
  code?: string[];
  extra?: (a: Architecture) => ReactNode;
};
type Layer = { name: string; note?: string; boxes: Box[] };

const n = (x: number | undefined) => (x ?? 0).toLocaleString("en-US");
const sum = (o: Record<string, number> | undefined, keys?: string[]) =>
  Object.entries(o ?? {}).reduce((s, [k, v]) => s + (!keys || keys.some((p) => k.startsWith(p)) ? v : 0), 0);

function Counts({ data }: { data: Record<string, number> }) {
  const rows = Object.entries(data);
  if (!rows.length) return <p className="muted">None yet at this point in the story.</p>;
  return (
    <table className="how-counts">
      <tbody>
        {rows.map(([k, v]) => (
          <tr key={k}>
            <td className="mono">{k}</td>
            <td className="num">{n(v)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const EVENT_FIELDS: Record<string, string> = {
  "deal.created": "deal, CRM fields, actor",
  "deal.updated": "deal, changed fields, actor (backdated edits keep their real known_at)",
  "contract.countersigned": "deal, timestamp, ACV, term, clauses",
  "contract.amended": "deal, new terms, signed_at",
  "invoice.issued": "Stripe id, deal, amount, date",
  "payment.received": "Stripe id, deal, amount, date",
  "refund.issued": "Stripe id, deal, amount, date",
  "payment.overdue": "deal, days late",
  "customer.churned": "deal, effective date, reason, refund",
  "rep.terminated": "rep, end date, state",
  "rep.leave": "rep, start date, state",
  "rep.territory_changed": "rep, accounts, effective date, actor",
  "plan.amended": "rep, version, changes, signed_at",
  "message.posted": "channel, author, recipient, text",
  "approval.decided": "approval, approver, decision, reason",
  "evidence.found": "finding, deal, account, document, quote, type",
  "period.lock_requested": "period",
  "clock.advance": "to",
  "job.due": "the close calendar: commission run, accrual, amortization, payroll file, churn review, digests",
};

const INVARIANTS: [string, string][] = [
  ["INV1", "every journal entry balances: debits = credits"],
  ["INV2", "nothing lands in a locked period; corrections post in the open one"],
  ["INV3", "every subledger ties to its GL account (2150, 1350, 1290, 1200)"],
  ["INV4", "no entry or commission line without a decision that lists its inputs"],
  ["INV5", "the requester never approves; the right approver, within their limit"],
  ["INV6", "the payroll file equals the approved lines, exactly"],
  ["INV7", "split credits on a deal add up to 100%"],
  ["INV8", "the hash chain verifies end to end"],
];

const LAYERS: Layer[] = [
  {
    name: "The company (Fernhill)",
    note: "Read-only connections. Every row is versioned and hashed; nothing here is ever edited in place.",
    boxes: [
      {
        title: "CRM",
        line: "Salesforce-style opportunities",
        status: "simulated",
        stat: (a) => `${n(a.tables.deals)} deal versions`,
        collects: ["account, CRM amount, close date, stage", "owner and split credits", "every edit as a new version with the time Quota learned it"],
        does: ["Feeds deals into the event queue (deal.created / deal.updated / territory moves)", "Never trusted alone: the contract wins for amount and booking date", "A close date moved into a locked quarter is flagged as backdated"],
        shown: [["Home → Sources → CRM", "#home"], ["Reps board", "#reps"], ["Trace source cards", "#trace"]],
        code: ["quota/sim/world.py (RecordsSystem)", "quota/sim/generator.py"],
      },
      {
        title: "E-signature + contracts",
        line: "countersign timestamp, ACV, term, clauses",
        status: "simulated",
        stat: (a) => `${n(a.tables.contracts)} contract versions · ${n(a.tables.esign_events)} e-sign events`,
        collects: ["sent, customer-signed and countersigned timestamps", "ACV, term, billing and termination clauses", "amendments as new versions"],
        does: ["Sets the booking date (countersignature) and the commissionable base (year-1 ACV)", "An amendment that raises ACV is booked for the increase only"],
        shown: [["Home → Sources → Contracts", "#home"], ["Rep detail → contract cards", "#reps"], ["Trace: the used field is highlighted", "#trace"]],
        code: ["quota/engine/ranking.py", "quota/engine/calculator.py"],
      },
      {
        title: "BambooHR clone",
        line: "real app, Bun/Prisma on :8080",
        status: "real",
        stat: (a) => `${n(a.tables.people)} people rows`,
        collects: ["roster, title, state, manager", "base pay, start and end dates"],
        does: ["Terminations and leaves become rep.terminated / rep.leave events", "Seeded from Quota's cast with `python -m quota export-roster`"],
        shown: [["Home → Sources → HR", "#home"], ["Header link: BambooHR", "#home"]],
        code: ["quota/hr/roster.py", "integrations/bamboohr-clone/"],
      },
      {
        title: "Stripe sandbox",
        line: "test mode, test clocks, Connect",
        status: "real",
        stat: (a) => `${n(a.tables.billing)} billing rows`,
        collects: ["invoices, payments, refunds (each with its Stripe id)", "Connect accounts and transfers for partners"],
        does: ["The billing mirror posts AR, cash and deferred revenue entries", "A duplicate payment webhook counts once", "Pay-on-collection: commission waits for the customer's payment"],
        shown: [["Home → Sources → Stripe", "#home"], ["Trace billing card with the Stripe id", "#trace"], ["Header link: Stripe sandbox", "#home"]],
        code: ["quota/sim/stripe_billing.py", "quota/integrations/stripe_connect.py"],
      },
      {
        title: "Q1 payout spreadsheet",
        line: "messy export, 140 rows, 3 of them wrong",
        status: "simulated",
        collects: ["last quarter's payouts as the team paid them"],
        does: ["Intake learns its columns and the signed plan's rules", "The Q1 backtest reproduces 137 of 140 rows and names the 3 errors"],
        shown: [["Home → Sources", "#home"], ["Intake tab", "#intake"]],
        code: ["quota/intake/"],
      },
      {
        title: "Email + Slack",
        line: "disputes, SPIF promises, side letters",
        status: "simulated",
        stat: (a) => `${n(a.evidence_docs)} indexed messages · ${n(a.tables.messages)} chat messages`,
        collects: ["reps' disputes and escalations", "managers' promises", "side letters that change a deal"],
        does: ["A model turns a message into a claim, never into a fact", "Every number in a claim must appear verbatim in the message", "Searched as evidence before a payout clears"],
        shown: [["Chat", "#chat"], ["Case → evidence", "#case"], ["Home → Sources → Email & Slack", "#home"]],
        code: ["quota/agent/extract.py", "quota/search/evidence.py"],
      },
    ],
  },
  {
    name: "Intake",
    boxes: [
      {
        title: "Computer-use agent",
        line: "reads the HR screens like a new hire",
        status: "in progress",
        does: ["Plan: read BambooHR's screens, write a playbook, re-sync later with 0 model calls", "Today: the roster export seeds the clone and Stripe Connect accounts"],
        shown: [["Header link: BambooHR", "#home"]],
        code: ["quota/hr/roster.py", "integrations/bamboohr-clone/"],
      },
      {
        title: "Intake + plan learning",
        line: "column synonyms, signed plans → rules",
        status: "built",
        does: ["Maps messy columns to Quota's fields", "Reads signed plan documents into rules", "Backtests Q1: 137 of 140 reproduced"],
        shown: [["Intake tab", "#intake"], ["Home", "#home"]],
        code: ["quota/intake/intake.py", "quota/intake/backtest.py"],
      },
    ],
  },
  {
    name: "Events",
    boxes: [
      {
        title: "Clocked event queue",
        line: "one simulated calendar, Jan–Jun 2026, company time (Pacific)",
        status: "built",
        stat: (a) => `${n(sum(a.events_by_type))} released · ${n(a.events_waiting)} waiting`,
        does: ["A fact exists only once the clock reaches it: no seat ever sees the future", "Each seat claims only the event kinds it handles; nobody prompts a seat", "Each pickup is recorded: handled, ignored, or failed (contained, never fatal)"],
        shown: [["Timeline (EVENT lines)", "#today"], ["Inject → preview and run", "#inject"]],
        code: ["quota/sim/clock_runner.py", "quota/sim/events.py"],
        extra: (a) => (
          <>
            <h4>The 19 event types and what each carries</h4>
            <table className="how-counts">
              <tbody>
                {Object.entries(EVENT_FIELDS).map(([type, fields]) => (
                  <tr key={type}>
                    <td className="mono">{type}</td>
                    <td>{fields}</td>
                    <td className="num">{a.events_by_type[type] ? n(a.events_by_type[type]) : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <h4>Pickups so far</h4>
            <Counts data={a.event_status} />
          </>
        ),
      },
    ],
  },
  {
    name: "The two seats",
    note: "They hand work to each other through the queue, like a human team. Neither can approve anything.",
    boxes: [
      {
        title: "seat:commissions_analyst",
        line: "commission lines, approval requests, messages, payroll files",
        status: "built",
        stat: (a) => `${n(a.tables.commission_lines)} commission lines`,
        does: [
          "Commission runs: one line per rep per booking, from the signed plan",
          "Disputes: re-checks the sources, then holds, concedes or offers escalation",
          "Plan exceptions: routes to the VP, over-limit to the Controller, records a precedent",
          "Holds: bad splits, collection, termination, territory moves, backdating, plan drift",
          "Payroll files, digests, follow-ups to the backup approver, clawback lines",
        ],
        shown: [["Chat replies", "#chat"], ["Case", "#case"], ["Today → done and held", "#today"]],
        code: ["quota/agent/commissions_analyst.py"],
        extra: (a) => (
          <>
            <h4>Commission lines by status</h4>
            <Counts data={a.line_statuses} />
          </>
        ),
      },
      {
        title: "seat:deferred_accountant",
        line: "journal entries under ASC 340-40",
        status: "built",
        stat: (a) => `${n(a.tables.journal_entries)} journal entries`,
        does: ["Accrues each commission (Dr 1350 / Cr 2150) and asks the Controller", "Amortizes straight-line over the contract term", "On churn or void: writes off the asset and books the recovery (1290)"],
        shown: [["Today + Ledger health", "#today"], ["Trace → JE cards", "#trace"]],
        code: ["quota/agent/deferred_accountant.py", "quota/engine/entries.py", "quota/engine/amortization.py"],
        extra: (a) => (
          <>
            <h4>Entries by status</h4>
            <Counts data={a.entries_by_status} />
          </>
        ),
      },
    ],
  },
  {
    name: "Seven steps, every event",
    note: "The split that makes it auditable: a model may read English and name a field; only deterministic code may produce a dollar, a date or an outcome.",
    boxes: [
      { title: "1. Pick up", line: "claims one event from its own queue", does: ["Nobody prompts a seat; the clock releases the work"], code: ["quota/agent/seat.py"] },
      { title: "2. Gather", line: "read-only; records table, id, version, sha256", does: ["Every row read is listed on the decision, so it can be replayed exactly"], shown: [["Trace → source records", "#trace"]] },
      {
        title: "3. Extract",
        line: "a model reads words; the quote must match verbatim",
        stat: (a) => `${n(a.decisions_with_model)} decisions used a model`,
        does: ["Claims from chat; facts from documents", "A number not in the quote fails extraction and earns one clarifying question"],
        code: ["quota/agent/extract.py", "quota/agent/llm.py"],
      },
      { title: "4. Reconcile", line: "source ranking decides which source wins", does: ["Booking date: precedent, countersignature, contract date, CRM close (never alone)", "Amount: signed contract over CRM", "Every disagreement listed with its dollar effect"], code: ["quota/engine/ranking.py"] },
      { title: "5. Calculate", line: "code decides dollars: rates, caps, 340-40", does: ["Tiers against quarterly attainment", "Amortization schedules, clawbacks, refunds"], code: ["quota/engine/calculator.py"] },
      {
        title: "6. Decide",
        line: "post | hold | escalate | correct",
        stat: (a) => `${n(sum(a.decisions_by_kind))} decisions`,
        does: ["Policy code picks the outcome; a model only words the reply"],
        extra: (a) => (
          <>
            <h4>Decisions by kind</h4>
            <Counts data={a.decisions_by_kind} />
            <h4>By outcome</h4>
            <Counts data={a.decisions_by_outcome} />
          </>
        ),
      },
      { title: "7. Write", line: "only through the WritePath", does: ["A refusal is logged as a numbered row, like any other write"], code: ["quota/ledger/write_path.py"] },
    ],
  },
  {
    name: "The only writer",
    boxes: [
      {
        title: "WritePath",
        line: "one transaction, one chain row per written row, or a logged refusal",
        status: "built",
        stat: (a) => `${n(a.tables.refusals)} refusals`,
        does: ["1. Capability check: a principal may only do what its seat allows (NO_CAPABILITY)", "2. Invariants on every write", "3. Write, or refuse and say why: every violated code, not just the first"],
        shown: [["Timeline → Refusals filter", "#today"], ["Ledger health", "#today"], ["Trace → refusal cards", "#trace"]],
        code: ["quota/ledger/write_path.py", "quota/ledger/invariants.py"],
        extra: (a) => (
          <>
            <h4>The eight invariants, live</h4>
            <table className="how-counts">
              <tbody>
                {INVARIANTS.map(([name, text]) => (
                  <tr key={name}>
                    <td className="mono">{name}</td>
                    <td>{text}</td>
                    <td className={a.invariants_ok[name] ? "ok" : "bad"}>{a.invariants_ok[name] ? "✓" : "✗"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <h4>Refusals by reason code</h4>
            <Counts data={a.refusals_by_code} />
          </>
        ),
      },
    ],
  },
  {
    name: "System of record",
    boxes: [
      {
        title: "Append-only, bitemporal, hash-chained SQLite",
        line: "24 tables; no row is ever updated or deleted",
        status: "built",
        stat: (a) => `${n(sum(a.tables))} rows · chain ${n(a.tables.chain)}`,
        does: ["Bitemporal: every row carries when Quota learned it, so any screen replays a past moment", "Hash chain: each row appends sha256(prev, row); `python -m quota verify` re-checks it from genesis", "Status tables: a line moves drafted → approved → paid → clawed back by appending rows"],
        shown: [["Trace → As of slider", "#trace"], ["Ledger health → chain", "#today"]],
        code: ["quota/ledger/schema.sql", "quota/ledger/hashing.py", "quota/ledger/queries.py"],
        extra: (a) => (
          <div className="how-groups">
            {Object.entries(a.table_groups).map(([group, names]) => (
              <div key={group}>
                <h4>{group}</h4>
                <Counts data={Object.fromEntries(names.map((t) => [t, a.tables[t] ?? 0]))} />
              </div>
            ))}
          </div>
        ),
      },
    ],
  },
  {
    name: "People and evidence",
    boxes: [
      {
        title: "The people it answers to",
        line: "people act only through the same chat and approval API",
        status: "built",
        stat: (a) => `${n(a.approvals.pending)} waiting · ${n(a.approvals.decided)} decided`,
        does: ["Approval limits per person, enforced in the write path", "Backup after 2 business days, or at once when someone is away", "Silence never approves"],
        shown: [["Chat → Speak as", "#chat"], ["Home → Needs you", "#home"]],
        code: ["quota/config.py", "quota/engine/routing.py", "quota/agent/personas.py"],
        extra: (a) => (
          <table className="how-counts">
            <tbody>
              {a.people.map((p) => (
                <tr key={p.id}>
                  <td>
                    <strong>{p.name}</strong> <span className="muted">{p.title || p.kind}</span>
                  </td>
                  <td className="muted">{p.capabilities.filter((c) => !c.startsWith("read") && c !== "message:send").join(", ") || "chat only"}</td>
                  <td className="num">{p.limit_cents !== null ? `limit ${usd(p.limit_cents)}` : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ),
      },
      {
        title: "Elastic evidence layer",
        line: "finds and explains; never decides a dollar",
        status: "real",
        does: [
          "quota-comms: the company's messages, semantic search (Jina embeddings and reranker)",
          "quota-decisions: a one-way, hash-verified mirror of the decision log",
          "quota-findings: what the scheduled promise sweep judged",
          "Quota auditor: an agent with read-only tools, also served over MCP",
          "Offline, a local full-text index answers the same questions",
        ],
        shown: [["Case → evidence", "#case"], ["Home → Sources → Email & Slack", "#home"]],
        code: ["quota/search/", "elastic/workflows/promise-sweep.yaml"],
      },
    ],
  },
  {
    name: "What the team gets",
    boxes: [
      { title: "Payroll", line: "one payout file per period", does: ["Every line traceable; a same-day final-pay file when a rep leaves", "Clawbacks netted against the next payout"], shown: [["Today", "#today"], ["Trace → payroll", "#trace"]] },
      { title: "Revenue Accounting", line: "ASC 340-40 accrual, amortization, write-offs", does: ["A subledger that ties to the GL (INV3)", "The churn cascade: one event, eight numbers"], shown: [["Ledger health", "#today"]] },
      { title: "Sales Ops", line: "rep statements and attainment", does: ["Disputes answered with evidence", "Proposed plan changes from repeated exceptions"], shown: [["Reps", "#reps"], ["Chat", "#chat"]] },
      { title: "The Controller", line: "ledger health, trial balance, period locks", does: ["A trace on every number back to its row and approver"], shown: [["Today + Ledger health", "#today"], ["Trace", "#trace"]] },
      { title: "Any auditor", line: "replay a decision from its recorded inputs", does: ["`python -m quota replay D-0005` recomputes it; the scorecard reports 100% replay match"], shown: [["Inject + Scorecard", "#inject"], ["Trace → Replay", "#trace"]] },
    ],
  },
];

function BoxCard({ box, data, open }: { box: Box; data: Architecture | null; open: boolean }) {
  return (
    <details className={`how-box ${box.status ? `st-${box.status.replace(" ", "-")}` : ""}`} open={open}>
      <summary>
        <span className="how-title">{box.title}</span>
        {box.status && <span className={`how-status st-${box.status.replace(" ", "-")}`}>{box.status}</span>}
        <span className="how-line">{box.line}</span>
        {data && box.stat && <span className="how-stat">{box.stat(data)}</span>}
      </summary>
      <div className="how-body">
        {box.collects && (
          <>
            <h4>What it collects</h4>
            <ul>{box.collects.map((c) => <li key={c}>{c}</li>)}</ul>
          </>
        )}
        {box.does && (
          <>
            <h4>What happens</h4>
            <ul>{box.does.map((c) => <li key={c}>{c}</li>)}</ul>
          </>
        )}
        {data && box.extra && box.extra(data)}
        {box.shown && (
          <>
            <h4>Where you see it</h4>
            <p className="how-links">
              {box.shown.map(([label, href]) => (
                <a key={label} href={href}>
                  {label}
                </a>
              ))}
            </p>
          </>
        )}
        {box.code && (
          <>
            <h4>In the code</h4>
            <p className="how-code">{box.code.map((c) => <code key={c}>{c}</code>)}</p>
          </>
        )}
      </div>
    </details>
  );
}

export function HowItWorks({ version }: { version: number }) {
  const [data, setData] = useState<Architecture | null>(null);
  const [mode, setMode] = useState<"closed" | "open">("closed");

  useEffect(() => {
    api.architecture().then(setData).catch(() => setData(null));
  }, [version]);

  return (
    <section className="how">
      <div className="how-head">
        <div>
          <h2>How Quota works</h2>
          <p className="muted">
            From the company's systems to the books, as the code runs it. Open any box for what it collects, what
            it does, where it shows up and where it lives. Numbers are live from this world
            {data && (
              <>
                {" "}as of <When ts={data.clock} />
              </>
            )}
            .
          </p>
        </div>
        <div className="how-toggle">
          <button onClick={() => setMode("open")}>Expand all</button>
          <button onClick={() => setMode("closed")}>Collapse all</button>
        </div>
      </div>
      {LAYERS.map((layer, i) => (
        <div key={layer.name}>
          {i > 0 && <div className="how-arrow" aria-hidden="true">↓</div>}
          <div className="how-layer">
            <div className="how-layer-name">{layer.name}</div>
            <div className="how-layer-body">
              {layer.note && <p className="how-note">{layer.note}</p>}
              <div className={`how-boxes ${layer.boxes.length > 3 ? "many" : ""}`}>
                {layer.boxes.map((box) => (
                  <BoxCard key={`${mode}-${box.title}`} box={box} data={data} open={mode === "open"} />
                ))}
              </div>
            </div>
          </div>
        </div>
      ))}
    </section>
  );
}
