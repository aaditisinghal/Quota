import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, isMissing, type ScenarioRun, type TimelineItem, type WorldState } from "./api";
import { CaseView } from "./Case";
import { ChatPane } from "./ChatPane";
import { HomeView } from "./Home";
import { InjectScore } from "./InjectScore";
import { IntakeView } from "./Intake";
import { Presenter } from "./Presenter";
import { RepView, RepsBoard } from "./Reps";
import { ScenarioMenu, ScenarioResult } from "./Scenarios";
import { Timeline } from "./Timeline";
import { TodayHealth } from "./TodayHealth";
import { TraceView } from "./TraceView";
import { When, learnAccountsFromText, setWorldYear, usd } from "./format";
import "./cfo.css";

type Tab = "home" | "case" | "reps" | "trace" | "chat" | "today" | "inject" | "intake";
const TABS: [Tab, string][] = [
  ["home", "Home"],
  ["case", "Case"],
  ["reps", "Reps"],
  ["chat", "Chat"],
  ["trace", "Trace"],
  ["today", "Today + Ledger health"],
  ["inject", "Inject + Scorecard"],
  ["intake", "Intake"],
];
// The source systems themselves, opened in a new tab: the HR clone seeded with Quota's roster (CUA_TASKS C1)
// and the Stripe test sandbox with the Orbit split's Connect transfer (C6).
const EXTERNAL: [string, string, string][] = [
  ["BambooHR", "http://127.0.0.1:8080/employees/directory.php", "The HR clone with Quota's people (run start-clone.cmd)"],
  ["Stripe sandbox", "https://dashboard.stripe.com/acct_1UTwgbGDis24t23a/test/connect/accounts/overview", "Stripe test mode: connected partners and transfers"],
];
// The CFO screens and the Case tab use the whole width; the others keep the Timeline on the right.
const WIDE: Tab[] = ["home", "case", "reps"];

/**
 * Where the hash points. `id` is the case line, the trace target, or the rep (on `reps`, null for the board);
 * `contract` and `calibrate` are the rep detail's focused contract and its calibration drawer; `source` is the
 * Home drawer. Routes: #home, #home/source/{kind}, #reps, #rep/{id}[/{contract}[/calibration]], #case[/CL-x],
 * #trace/{x} (x may itself contain a slash: deal/D-1), #chat, #today, #inject, #intake.
 */
type Route = { tab: Tab; id: string | null; contract: string | null; calibrate: boolean; source: string | null };

function decode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

function parse(raw: string): Route | null {
  const plain: Route = { tab: "home", id: null, contract: null, calibrate: false, source: null };
  const hash = decode(raw);
  if (hash.startsWith("trace/")) return { ...plain, tab: "trace", id: hash.slice(6) };
  if (hash.startsWith("case/")) return { ...plain, tab: "case", id: hash.slice(5) };
  const parts = raw.split("/").map(decode);
  if (parts[0] === "home" && parts[1] === "source" && parts[2]) return { ...plain, source: parts[2] };
  if (parts[0] === "rep" && parts[1]) {
    return { ...plain, tab: "reps", id: parts[1], contract: parts[2] || null, calibrate: !!parts[2] && parts[3] === "calibration" };
  }
  if (TABS.some(([key]) => key === hash)) return { ...plain, tab: hash as Tab };
  return null;
}

function route(): Route {
  return parse(window.location.hash.slice(1)) ?? { tab: "home", id: null, contract: null, calibrate: false, source: null };
}

export function App() {
  const [{ tab, id, contract, calibrate, source }, setRoute] = useState(route);
  const [traceId, setTraceId] = useState("CL-0001");
  const [caseLine, setCaseLine] = useState("CL-0001");
  const [state, setState] = useState<WorldState | null>(null);
  const [items, setItems] = useState<TimelineItem[]>([]);
  const [version, setVersion] = useState(0);
  const [principal, setPrincipal] = useState("jordan.kim");
  const [offline, setOffline] = useState(false);
  const [presenting, setPresenting] = useState(() => {
    try {
      return sessionStorage.getItem("quota.presenting") === "1" || /[?&]present\b/.test(window.location.search);
    } catch {
      return false;
    }
  });
  useEffect(() => {
    try {
      sessionStorage.setItem("quota.presenting", presenting ? "1" : "0");
    } catch {
      // not kept in a private window
    }
  }, [presenting]);
  const count = useRef(0);

  useEffect(() => {
    const onHash = () => setRoute(route());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  useEffect(() => {
    if (id && tab === "trace") setTraceId(id);
    if (id && tab === "case") setCaseLine(id);
  }, [id, tab]);

  // `full` reloads the whole timeline: a restored checkpoint (presenter, scenario, reset) rewinds it, so lines
  // seen before may no longer be true. A shorter timeline than we hold means someone else rewound it.
  const generation = useRef(0);
  const refresh = useCallback(async (full = false) => {
    if (full) generation.current += 1;
    const mine = generation.current;
    try {
      let [world, timeline] = await Promise.all([api.state(), api.timeline(full ? 0 : count.current)]);
      if (!full && world.timeline_length < count.current) {
        full = true;
        generation.current += 1;
        timeline = await api.timeline(0);
      }
      if (mine !== generation.current && !full) return; // a full reload started since; its answer wins
      timeline.items.forEach((i) => i.kind === "FACT" && learnAccountsFromText(i.text));
      if (full) {
        count.current = timeline.items.length ? timeline.items[timeline.items.length - 1].seq : 0;
        setItems(timeline.items);
      } else if (timeline.items.length) {
        count.current = Math.max(count.current, timeline.items[timeline.items.length - 1].seq);
        // Two refreshes can overlap (a poll and an action); keep each line once.
        setItems((old) => {
          const last = old.length ? old[old.length - 1].seq : 0;
          return [...old, ...timeline.items.filter((i) => i.seq > last)];
        });
      }
      setWorldYear(world.clock);
      setState(world);
      setOffline(false);
      setVersion((v) => v + 1);
    } catch {
      setOffline(true);
    }
  }, []);
  const update = useCallback(() => refresh(), [refresh]);
  const resync = useCallback(() => refresh(true), [refresh]);

  // Presenter mode polls faster (R7.6), so what the room sees keeps up with the narration.
  useEffect(() => {
    refresh();
    const timer = window.setInterval(() => refresh(), presenting ? 1500 : 5000);
    return () => window.clearInterval(timer);
  }, [refresh, presenting]);

  // The Intake tab shows only on a server with an inbox (--from-inbox).
  const [hasIntake, setHasIntake] = useState(false);
  useEffect(() => {
    api
      .intake()
      .then(() => setHasIntake(true))
      .catch((e) => isMissing(e) && setHasIntake(false));
  }, []);
  const [menu, setMenu] = useState(false);
  const [ran, setRan] = useState<ScenarioRun | null>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setMenu((m) => !m);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const onRan = useCallback(
    (run: ScenarioRun, to: string) => {
      setRan(run);
      if (run.name === "impersonation") setPrincipal("jordan.kim");
      resync();
      window.location.hash = to;
    },
    [resync],
  );

  const onMoved = useCallback(
    (to?: string) => {
      resync();
      // A presenter step may name a tab or a deeper route (rep/jordan.kim/D-1/calibration, home/source/crm).
      if (to && parse(to.replace(/^#/, ""))) window.location.hash = to.replace(/^#/, "");
    },
    [resync],
  );

  const pending = state?.pending_approvals ?? [];
  const pendingIds = useMemo(() => new Set(pending.map((a) => a.id)), [pending]);
  const stops = useMemo(() => Array.from(new Set(items.map((i) => i.clock))).sort(), [items]);
  const people = state?.principals ?? [];
  const waiting = pending.reduce((s, a) => s + a.amount_cents, 0);

  return (
    <div className={`app ${presenting ? "presenting" : ""}`}>
      <header className="top">
        <div className="brand">
          <img className="logo" src="./logo.png" alt="Quota" width="30" height="30" />
          <div>
            <h1>Quota</h1>
            <span className="muted">
              commissions desk · {state ? `${state.world}${state.seed !== null ? ` seed ${state.seed}` : ""}, ${state.mode}` : "…"}
            </span>
          </div>
        </div>
        <div className="clock" title="Simulated clock">
          <span className="muted">Clock</span>
          <strong>{state ? <When ts={state.clock} /> : "…"}</strong>
        </div>
        {pending.length > 0 && (
          <a className="badge accent big-badge" href="#today">
            {pending.length} waiting on people · {usd(waiting)}
          </a>
        )}
        {offline && <span className="badge">API unreachable</span>}
        <button onClick={() => setMenu(true)} title="Run a rehearsed scenario (⌘K / Ctrl+K)">
          Scenarios <kbd>{/Mac|iPhone|iPad/.test(navigator.platform) ? "⌘K" : "Ctrl K"}</kbd>
        </button>
        <button className={presenting ? "on" : ""} onClick={() => setPresenting((p) => !p)} title="Presenter mode: Back, Next, Reset">
          {presenting ? "Presenting" : "Present"}
        </button>
      </header>
      <nav className="tabs" role="tablist">
        {TABS.filter(([key]) => key !== "intake" || hasIntake || tab === "intake").map(([key, label]) => (
          <a key={key} role="tab" aria-selected={tab === key} className={`tab ${tab === key ? "on" : ""}`}
             href={key === "trace" ? `#trace/${encodeURIComponent(traceId)}` : `#${key}`}>
            {label}
          </a>
        ))}
        <span className="nav-external">
          {EXTERNAL.map(([label, href, title]) => (
            <a key={label} className="ext-link" href={href} target="_blank" rel="noreferrer" title={title}>
              {label} ↗
            </a>
          ))}
        </span>
      </nav>
      <div className={`body ${WIDE.includes(tab) ? "wide" : ""}`}>
        <main>
          {ran && <ScenarioResult run={ran} onClose={() => setRan(null)} />}
          {tab === "home" && <HomeView people={people} version={version} onChange={update} source={source} />}
          {tab === "reps" && !id && <RepsBoard version={version} />}
          {tab === "reps" && id && (
            <RepView key={id} rep={id} contract={contract} calibrate={calibrate} people={people} version={version} onChange={update} />
          )}
          {tab === "case" && (
            <CaseView line={caseLine} items={items} people={people} pending={pending} principal={principal}
                      setPrincipal={setPrincipal} away={state?.away ?? {}} version={version} onChange={update} />
          )}
          {tab === "trace" && (
            <TraceView id={traceId} stops={stops} people={people}
                       onOpen={(next) => (window.location.hash = `trace/${encodeURIComponent(next)}`)} />
          )}
          {tab === "chat" && (
            <ChatPane principal={principal} setPrincipal={setPrincipal} people={people} pending={pending}
                      away={state?.away ?? {}}
                      version={version} onChange={update} />
          )}
          {tab === "today" && <TodayHealth version={version} people={people} />}
          {tab === "intake" && <IntakeView people={people} version={version} onChange={update} />}
          {tab === "inject" && state && (
            <InjectScore clock={state.clock} people={people} world={state.world} seed={state.seed} onChange={update} />
          )}
        </main>
        {!WIDE.includes(tab) && <Timeline items={items} people={people} pending={pendingIds} />}
      </div>
      {presenting && <Presenter onMoved={onMoved} onClose={() => setPresenting(false)} paused={menu} />}
      <ScenarioMenu open={menu} onClose={() => setMenu(false)} onRan={onRan} />
    </div>
  );
}
