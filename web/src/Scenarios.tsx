import { useEffect, useMemo, useRef, useState } from "react";
import { api, isMissing, type Scenario, type ScenarioRun } from "./api";
import { Reply } from "./format";

// The rehearsed scenarios (NEXT_TASKS R4), by phrase. ⌘K / Ctrl+K opens the menu; choosing one runs it from its
// checkpoint and switches to the tab where its result shows.

// Where each scenario's result is easiest to see; a run may name its own tab instead.
export const SCENARIO_TAB: Record<string, string> = {
  birch_churns: "today",
  tz_acme: "case",
  morgan_approves: "case",
  jordan_quits: "today",
  approve_yourself: "case",
  impersonation: "chat",
  priya_rejects: "case",
  short_pay_98: "today",
};

type Props = { open: boolean; onClose: () => void; onRan: (run: ScenarioRun, tab: string) => void };

export function ScenarioMenu({ open, onClose, onRan }: Props) {
  const [scenarios, setScenarios] = useState<Scenario[] | null>(null);
  const [missing, setMissing] = useState(false);
  const [filter, setFilter] = useState("");
  const [active, setActive] = useState(0);
  const [running, setRunning] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setFilter("");
    setActive(0);
    setError(null);
    input.current?.focus();
    api
      .scenarios()
      .then((list) => {
        setScenarios(list);
        setMissing(false);
      })
      .catch((e) => (isMissing(e) ? setMissing(true) : setError((e as Error).message)));
  }, [open]);

  const shown = useMemo(() => {
    const words = filter.toLowerCase().split(/\s+/).filter(Boolean);
    return (scenarios ?? []).filter((s) => words.every((w) => `${s.phrase} ${s.name}`.toLowerCase().includes(w)));
  }, [scenarios, filter]);

  async function run(s: Scenario) {
    if (running) return;
    setRunning(s.name);
    setError(null);
    try {
      const result = await api.runScenario(s.name);
      const tab = typeof result.result.tab === "string" ? result.result.tab : SCENARIO_TAB[s.name] ?? "case";
      onRan(result, tab);
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRunning(null);
    }
  }

  if (!open) return null;
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="palette" role="dialog" aria-modal="true" aria-label="Scenarios">
        <input
          ref={input}
          value={filter}
          placeholder="Run a scenario…"
          onChange={(e) => {
            setFilter(e.target.value);
            setActive(0);
          }}
          onKeyDown={(e) => {
            if (e.key === "Escape") onClose();
            else if (e.key === "ArrowDown") setActive((a) => Math.min(a + 1, shown.length - 1));
            else if (e.key === "ArrowUp") setActive((a) => Math.max(a - 1, 0));
            else if (e.key === "Enter" && shown[active]) run(shown[active]);
            else return;
            e.preventDefault();
          }}
          aria-label="Filter scenarios"
        />
        {missing && <p className="muted palette-note">Scenarios are not available on this server (no /scenarios).</p>}
        {error && <p className="error palette-note">{error}</p>}
        {!scenarios && !missing && !error && <p className="muted palette-note">Loading…</p>}
        <ul className="palette-list" role="listbox">
          {shown.map((s, i) => (
            <li key={s.name} role="option" aria-selected={i === active}>
              <button className={`palette-item ${i === active ? "on" : ""}`} onMouseEnter={() => setActive(i)}
                      onClick={() => run(s)} disabled={running !== null}>
                <span className="palette-phrase">“{s.phrase}”</span>
                <span className="muted">
                  {running === s.name ? "running…" : `${s.name} · from ${s.checkpoint}`}
                </span>
              </button>
            </li>
          ))}
          {scenarios && shown.length === 0 && <li className="muted palette-note">No scenario matches.</li>}
        </ul>
        <p className="muted palette-foot">↑↓ choose · Enter run · Esc close</p>
      </div>
    </div>
  );
}

/** The last scenario's result, above the screen it switched to. */
export function ScenarioResult({ run, onClose }: { run: ScenarioRun; onClose: () => void }) {
  return (
    <div className={`scenario-result ${run.result.ok ? "ok" : "bad"}`} role="status">
      <span className="scenario-mark">{run.result.ok ? "✓" : "✗"}</span>
      <div>
        <strong>Scenario {run.name}</strong>
        <div>
          <Reply text={run.result.summary} />
        </div>
      </div>
      <button className="link" onClick={onClose} aria-label="Dismiss">
        ✕
      </button>
    </div>
  );
}
