"""Event queue: release events where release_at <= clock.now().

Future world events (the script) wait in memory and are appended to `events` only when their time comes,
so the ledger never knows about something before it happens. Timer sources (e.g. "remind approvers at
09:00Z") are asked fresh every time, so a timer whose reason went away never fires.
"""

import json

from quota.ledger import queries
from quota.sim import events as ev

SYSTEM = "system"


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class ClockRunner:
    def __init__(self, ro, wp, clock, emit=None):
        self.ro, self.wp, self.clock = ro, wp, clock
        self.emit = emit
        self._scheduled: list[tuple[str, int, ev.Event]] = []
        self._timer_sources = []

    # ---------- inputs ----------

    def schedule(self, at: str, event_type: str, payload: dict) -> None:
        """A future world event; validated now, appended to the ledger when the clock reaches `at`."""
        self._scheduled.append((at, len(self._scheduled), ev.make(event_type, payload, at)))
        self._scheduled.sort()

    def add_timer_source(self, source) -> None:
        """source(now) -> [(at, event_type, payload)] for events that should exist once the clock reaches at."""
        self._timer_sources.append(source)

    def emit_now(self, event_type: str, payload: dict, created_by: str = SYSTEM) -> ev.Event:
        """A notification released immediately (message posted, approval decided)."""
        return self._append(ev.make(event_type, payload, self.clock.now()), created_by)

    # ---------- release ----------

    def _append(self, event: ev.Event, created_by: str = SYSTEM) -> ev.Event:
        row = self.wp.append_fact("events", {"type": event.type, "payload_json": event.payload,
                                             "release_at": event.release_at, "created_by": created_by}, SYSTEM)
        return ev.Event.from_row(row)

    def _already_released(self, event_type, payload) -> bool:
        return queries.fetch_one(self.ro, "SELECT id FROM events WHERE type = ? AND payload_json = ?",
                                 (event_type, _canonical(payload))) is not None

    def _due_timers(self, now) -> list[ev.Event]:
        due = []
        for source in self._timer_sources:
            for at, event_type, payload in source(now):
                if at <= now and not self._already_released(event_type, payload):
                    due.append(ev.make(event_type, payload, at))
        return sorted(due, key=lambda e: (e.release_at, _canonical(e.payload)))

    def release_due(self) -> list[ev.Event]:
        """Append every scheduled event and timer whose time has come; returns what was released."""
        now = self.clock.now()
        released = []
        while self._scheduled and self._scheduled[0][0] <= now:
            released.append(self._append(self._scheduled.pop(0)[2]))
        released += [self._append(timer) for timer in self._due_timers(now)]
        if self.emit:
            for event in released:
                self.emit(SYSTEM, "EVENT", f"{event.id} {ev.describe(event)}")
        return released

    # ---------- consumption ----------

    def pending(self, actor: str, types) -> list[ev.Event]:
        """Released events of these types that `actor` has not picked up yet, oldest first."""
        types = sorted(types)
        if not types:
            return []
        now = self.clock.now()
        rows = queries.fetch_all(self.ro, f"""
            SELECT * FROM events e
            WHERE e.type IN ({", ".join("?" * len(types))}) AND e.release_at <= ? AND e.known_at <= ?
              AND NOT EXISTS (SELECT 1 FROM event_status s WHERE s.event_id = e.id AND s.actor = ?)
            ORDER BY e.release_at, e.id""", (*types, now, now, actor))
        return [ev.Event.from_row(r) for r in rows]

    def next_for(self, actor: str, types) -> ev.Event | None:
        pending = self.pending(actor, types)
        return pending[0] if pending else None

    def next_time(self) -> str | None:
        """When something next becomes due: a scheduled event, a future-dated event, or a timer."""
        now = self.clock.now()
        candidates = [at for at, _, _ in self._scheduled if at > now]
        future = queries.fetch_one(self.ro, "SELECT MIN(release_at) AS t FROM events WHERE release_at > ?", (now,))
        if future and future["t"]:
            candidates.append(future["t"])
        for source in self._timer_sources:
            candidates += [at for at, event_type, payload in source(now)
                           if at > now and not self._already_released(event_type, payload)]
        return min(candidates) if candidates else None
