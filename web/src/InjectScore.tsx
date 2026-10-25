import { useEffect, useState } from "react";
import { api, type InjectEvent, type Parsed, type Principal, type Scorecard, type TimelineItem } from "./api";
import { Money, When, usd } from "./format";
import { TimelineRow } from "./Timeline";

// SPEC "Judge phrases to rehearse", plus moving the clock.
const EXAMPLES = [
  "Acme churns in June and wants its money back",
  "Jordan says his manager promised him a SPIF",
  "The CFO says pay Taylor anyway",
  "Change Alex's split to 70/40",
  "Riley moves the close date back to March 30",
  "Acme never pays",
  "The Controller is on vacation",
  "Approve your own payout",
  "Advance the clock to May 15",
  "Jordan goes on leave on May 4",
];

const ERROR_TYPES: Record<string, string> = {
  E01: "CRM amount above contract ACV",
  E02: "Splits add to 110%",
  E03: "Close Mar 31, countersigned Apr 2; rep disputes",
  E04: "Close date backdated into a locked quarter",
  E05: "SPIF promised by a manager in chat",
  E06: "Multi-year deal entered on TCV",
  E07: "Customer 120 days late",
  E08: "Churn inside the clawback window",
  E09: "Rep terminated (California)",
  E10: "Territory move half done by a bot",
  E11: "Same deal credited 100% to two reps",
  E12: "Amendment raises ACV mid-term",
  E13: "Duplicate payment webhook",
  E14: "Unsigned plan draft differs from signed plan",
  E15: "Deal voided after payout",
  E16: "Payroll export differs from approved lines",
};

type Props = { clock: string; people: Principal[]; world: string; seed: number | null; onChange: () => void };

export function InjectScore({ clock, people, world, seed, onChange }: Props) {
  return (
    <section className="inject-score">
      <Injector clock={clock} people={people} onChange={onChange} />
      <ScorecardPanel world={world} seed={seed} />
    </section>
  );
}

function Injector({ clock, people, onChange }: { clock: string; people: Principal[]; onChange: () => void }) {
  const [text, setText] = useState("");
  const [parsed, setParsed] = useState<Parsed | null>(null);
  const [drafts, setDrafts] = useState<string[]>([]);
  const [ran, setRan] = useState<TimelineItem[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function preview(sentence: string) {
    setBusy(true);
    setError(null);
    setRan(null);
    try {
      const result = await api.parse(sentence);
      setParsed(result);
      setDrafts("events" in result ? result.events.map((e) => JSON.stringify({ at: e.at, type: e.type, payload: e.payload }, null, 2)) : []);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function run() {
    setBusy(true);
    setError(null);
    try {
      const events = drafts.map((d) => JSON.parse(d) as InjectEvent);
      const result = await api.run(events);
      setRan(result.timeline);
      setParsed(null);
      setText("");
      onChange();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <h2>
        Scenario injector <span className="muted">clock <When ts={clock} /></span>
      </h2>
      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          if (text.trim()) preview(text.trim());
        }}
      >
        <textarea value={text} onChange={(e) => setText(e.target.value)} rows={2} placeholder="Say what happens in the world…" />
        <button className="primary" type="submit" disabled={busy || !text.trim()}>
          Preview
        </button>
      </form>
      <div className="chips">
        {EXAMPLES.map((x) => (
          <button key={x} className="chip" onClick={() => (setText(x), preview(x))}>
            {x}
          </button>
        ))}
      </div>
      {error && <p className="error">{error}</p>}

      {parsed && "unmapped" in parsed && (
        <div className="card">
          <h3>Couldn't map that</h3>
          <p>
            “{parsed.unmapped}”{parsed.reason ? `: ${parsed.reason}` : ""}. Quota never invents a new event type. Closest phrasings:
          </p>
          <div className="chips">
            {parsed.closest.map((c) => (
              <button key={c} className="chip" onClick={() => (setText(c), preview(c))}>
                {c}
              </button>
            ))}
          </div>
        </div>
      )}

      {parsed && "events" in parsed && (
        <div className="preview">
          <h3>
            Parsed by {parsed.source === "model" ? "the model (strict schema)" : "fixed phrases"}; amounts and times computed by code.
            Edit a field, then run.
          </h3>
          {parsed.events.map((e, i) => (
            <div key={i} className="card event">
              <div className="card-head">
                <span className="mono">{e.type}</span> <span className="muted">at <When ts={e.at} /></span>
              </div>
              <p>{e.summary}</p>
              <textarea
                className="json"
                value={drafts[i]}
                rows={Math.min(12, drafts[i]?.split("\n").length ?? 4)}
                onChange={(ev) => setDrafts(drafts.map((d, j) => (j === i ? ev.target.value : d)))}
                spellCheck={false}
              />
            </div>
          ))}
          <button className="primary" disabled={busy} onClick={run}>
            {busy ? "Running…" : "Run"}
          </button>
        </div>
      )}

      {ran && (
        <div className="turn-events">
          <h3>What happened ({ran.length} timeline items)</h3>
          <ol className="tl-list">
            {ran.map((item) => (
              <TimelineRow key={item.seq} item={item} people={people} />
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}

function ScorecardPanel({ world, seed }: { world: string; seed: number | null }) {
  const [card, setCard] = useState<Scorecard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const shown = seed ?? 7;
  // Case amounts link into the trace only when this server is running that same generated world.
  const linkable = world === "fernhill";

  useEffect(() => {
    setCard(null);
    api.scorecard(shown).then(setCard).catch((e: Error) => setError(e.message));
  }, [shown]);

  if (error) return <div className="panel"><p className="error">{error}</p></div>;
  if (!card) return <div className="panel"><h2>Scorecard</h2><p className="muted">Loading the eval for seed {shown} (runs it once if needed)…</p></div>;
  const pct = (x: number) => `${(100 * x).toFixed(1)}%`;
  return (
    <div className="panel">
      <h2>
        Scorecard <span className="muted">seed {card.seed}, {card.reps} reps, {card.events} events, from {card.source}</span>
      </h2>
      <div className="tiles">
        <Tile label="Planted errors caught" value={`${card.correct} / ${card.planted}`} note="outcome and dollars both match" />
        <Tile label="False flags" value={String(card.false_flags)} />
        <Tile label="Wrong automatic payouts" value={String(card.wrong_automatic_payouts)} />
        {card.pushback && (
          <Tile
            label="Pushback test"
            value={`${card.pushback.held_invalid}/${card.pushback.invalid} · ${card.pushback.conceded_valid}/${card.pushback.valid}`}
            note="held invalid · conceded valid"
          />
        )}
        <Tile label="Straight-through" value={pct(card.straight_through)} />
        <Tile label="Escalations / 100 events" value={card.escalations_per_100_events.toFixed(1)} />
        <Tile label="Replay match" value={pct(card.replay_match)} note={`${card.decisions} decisions`} />
        <Tile
          label="Write-path refusals"
          value={String(card.refusals)}
          note={Object.entries(card.refusal_codes).map(([c, n]) => `${c} ×${n}`).join(", ")}
        />
      </div>
      <h3>Planted cases {card.misses.length ? `(${card.misses.length} missed)` : "(no misses)"}</h3>
      <div className="table-wrap">
        <table className="table cases">
          <thead>
            <tr>
              <th>Case</th>
              <th>Error</th>
              <th>Deal</th>
              <th>Expected</th>
              <th>Got</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {card.cases.slice().sort((a, b) => a.case_id.localeCompare(b.case_id)).map((c) => (
              <tr key={c.case_id} className={c.correct ? "" : "miss"}>
                <td className="mono">{c.case_id}</td>
                <td>{ERROR_TYPES[c.type] ?? c.type}</td>
                <td className="mono">{c.deal_id}</td>
                <td>
                  {c.expected_outcome}{" "}
                  {linkable ? <Money cents={c.expected_amount_cents} trace={`deal/${c.deal_id}`} /> : usd(c.expected_amount_cents)}
                </td>
                <td>
                  {c.got_outcome} {linkable ? <Money cents={c.got_amount_cents} trace={`deal/${c.deal_id}`} /> : usd(c.got_amount_cents)}
                </td>
                <td className={c.correct ? "ok" : "bad"}>{c.correct ? "✓" : "✗ miss"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!linkable && (
        <p className="muted">
          This server runs the Acme world, so case amounts are plain text. Serve the generated world
          (<code>python -m quota serve --world fernhill --seed {card.seed}</code>) to click any case into its trace.
        </p>
      )}
    </div>
  );
}

function Tile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="tile">
      <div className="tile-value">{value}</div>
      <div className="tile-label">{label}</div>
      {note && <div className="tile-note">{note}</div>}
    </div>
  );
}
