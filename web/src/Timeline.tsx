import { useMemo, useState } from "react";
import type { Principal, TimelineItem } from "./api";
import { Reply, When, personName } from "./format";

const FILTERS: Record<string, (item: TimelineItem) => boolean> = {
  All: () => true,
  Decisions: (i) => i.kind === "DECISION" || i.kind === "APPROVAL",
  Entries: (i) => i.kind === "ENTRY" || i.kind === "PAYROLL",
  Messages: (i) => i.kind === "MESSAGE" || i.kind === "REPLY",
  Refusals: (i) => i.kind === "REFUSED",
};

/** The printed timeline, newest first. Refusals are first-class items with their reason codes. */
export function Timeline({ items, people, pending }: { items: TimelineItem[]; people: Principal[]; pending: Set<string> }) {
  const [filter, setFilter] = useState("All");
  const shown = useMemo(() => items.filter(FILTERS[filter]).slice().reverse(), [items, filter]);
  return (
    <aside className="timeline" aria-label="Timeline">
      <div className="timeline-head">
        <h2>Timeline</h2>
        <div className="chips" role="tablist">
          {Object.keys(FILTERS).map((f) => (
            <button key={f} className={`chip ${f === filter ? "on" : ""}`} onClick={() => setFilter(f)}>
              {f}
            </button>
          ))}
        </div>
      </div>
      <ol className="tl-list">
        {shown.map((item) => (
          <TimelineRow key={item.seq} item={item} people={people} human={opens(item, pending)} />
        ))}
        {shown.length === 0 && <li className="empty">Nothing yet.</li>}
      </ol>
    </aside>
  );
}

/** The line that put a still-pending approval in someone's queue: the request itself, or the routing on. */
function opens(item: TimelineItem, pending: Set<string>): boolean {
  if (item.kind !== "APPROVAL") return false;
  return [...pending].some((id) => item.text.startsWith(`${id} `) || item.text.includes(`; ${id} -> `));
}

export function TimelineRow({ item, people, human }: { item: TimelineItem; people: Principal[]; human?: boolean }) {
  return (
    <li className={`tl kind-${item.kind.toLowerCase()} ${human ? "human" : ""}`}>
      <div className="tl-meta">
        <When ts={item.clock} />
        <span className="actor">{personName(item.actor, people)}</span>
        <span className="kind">{item.kind}</span>
        {human && <span className="badge accent">needs a person</span>}
      </div>
      <div className="tl-text">
        <Reply text={item.text} />
      </div>
      {item.codes && (
        <div className="codes">
          {item.codes.map((c) => (
            <code key={c} className="code">
              {c}
            </code>
          ))}
        </div>
      )}
    </li>
  );
}
