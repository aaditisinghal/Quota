import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type TimelineItem, type WorldState } from "./api";
import { CaseView } from "./Case";
import { ChatPane } from "./ChatPane";
import { InjectScore } from "./InjectScore";
import { Timeline } from "./Timeline";
import { TodayHealth } from "./TodayHealth";
import { TraceView } from "./TraceView";
import { When, learnAccountsFromText, setWorldYear, usd } from "./format";

type Tab = "case" | "trace" | "chat" | "today" | "inject";
const TABS: [Tab, string][] = [
  ["case", "Case"],
  ["chat", "Chat"],
  ["trace", "Trace"],
  ["today", "Today + Ledger health"],
  ["inject", "Inject + Scorecard"],
];

function route(): { tab: Tab; id: string | null } {
  const hash = decodeURIComponent(window.location.hash.slice(1));
  if (hash.startsWith("trace/")) return { tab: "trace", id: hash.slice(6) };
  if (hash.startsWith("case/")) return { tab: "case", id: hash.slice(5) };
  if (TABS.some(([key]) => key === hash)) return { tab: hash as Tab, id: null };
  return { tab: "case", id: null };
}

export function App() {
  const [{ tab, id }, setRoute] = useState(route);
  const [traceId, setTraceId] = useState("CL-0001");
  const [caseLine, setCaseLine] = useState("CL-0001");
  const [state, setState] = useState<WorldState | null>(null);
  const [items, setItems] = useState<TimelineItem[]>([]);
  const [version, setVersion] = useState(0);
  const [principal, setPrincipal] = useState("jordan.kim");
  const [offline, setOffline] = useState(false);
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

  const refresh = useCallback(async () => {
    try {
      const [world, timeline] = await Promise.all([api.state(), api.timeline(count.current)]);
      timeline.items.forEach((i) => i.kind === "FACT" && learnAccountsFromText(i.text));
      if (timeline.items.length) {
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

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const pending = state?.pending_approvals ?? [];
  const pendingIds = useMemo(() => new Set(pending.map((a) => a.id)), [pending]);
  const stops = useMemo(() => Array.from(new Set(items.map((i) => i.clock))).sort(), [items]);
  const people = state?.principals ?? [];
  const waiting = pending.reduce((s, a) => s + a.amount_cents, 0);

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <span className="logo">Q</span>
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
      </header>
      <nav className="tabs" role="tablist">
        {TABS.map(([key, label]) => (
          <a key={key} role="tab" aria-selected={tab === key} className={`tab ${tab === key ? "on" : ""}`}
             href={key === "trace" ? `#trace/${encodeURIComponent(traceId)}` : `#${key}`}>
            {label}
          </a>
        ))}
      </nav>
      <div className={`body ${tab === "case" ? "wide" : ""}`}>
        <main>
          {tab === "case" && (
            <CaseView line={caseLine} items={items} people={people} pending={pending} principal={principal}
                      setPrincipal={setPrincipal} away={state?.away ?? {}} version={version} onChange={refresh} />
          )}
          {tab === "trace" && (
            <TraceView id={traceId} stops={stops} people={people}
                       onOpen={(next) => (window.location.hash = `trace/${encodeURIComponent(next)}`)} />
          )}
          {tab === "chat" && (
            <ChatPane principal={principal} setPrincipal={setPrincipal} people={people} pending={pending}
                      away={state?.away ?? {}}
                      version={version} onChange={refresh} />
          )}
          {tab === "today" && <TodayHealth version={version} people={people} />}
          {tab === "inject" && state && (
            <InjectScore clock={state.clock} people={people} world={state.world} seed={state.seed} onChange={refresh} />
          )}
        </main>
        {tab !== "case" && <Timeline items={items} people={people} pending={pendingIds} />}
      </div>
    </div>
  );
}
