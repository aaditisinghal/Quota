// Types and calls for the Quota API (quota/api/app.py). Money is integer cents everywhere.

export type Principal = {
  id: string;
  name: string;
  title: string;
  capabilities: string[];
  limit_cents: number | null;
};

export type Pending = {
  id: string;
  kind: string;
  subject: string;
  approver: string;
  amount_cents: number;
  since: string;
};

export type WorldState = {
  world: string;
  mode: string;
  seed: number | null;
  clock: string;
  timeline_length: number;
  principals: Principal[];
  pending_approvals: Pending[];
  upcoming: { at: string; type: string; payload: Record<string, unknown> }[];
  away: Record<string, string>; // principal -> back at (an approver on vacation answers nothing)
};

export type TimelineItem = {
  seq: number;
  clock: string;
  actor: string;
  kind: string;
  text: string;
  line: string;
  refs: string[];
  codes?: string[];
};

export type TraceNode = {
  id: string;
  kind: string;
  label: string;
  amount_cents?: number | null;
  known_at?: string | null;
  status?: string | null;
  [field: string]: unknown;
};

export type TraceEdge = { from: string; to: string; label: string };

export type Trace = {
  root: string | null;
  requested: string;
  kind: string;
  as_of: string | null;
  known: boolean;
  nodes: TraceNode[];
  edges: TraceEdge[];
};

export type Health = {
  clock: string;
  invariants: { name: string; ok: boolean; violations: { subject: string; detail: string }[] }[];
  chain: { ok: boolean; rows: number; first_bad_seq: number | null };
  trial_balance: {
    accounts: { account: string; name: string; balance_cents: number }[];
    debits_cents: number;
    credits_cents: number;
  };
  tie_outs: { name: string; subledger: string; gl_cents: number; subledger_cents: number; ok: boolean }[];
  cascades: { deal: string; items: { label: string; text: string; trace: string | null }[] }[];
  periods: { period: string; status: string; since: string; by: string }[];
};

export type Today = {
  clock: string;
  day: string;
  done: { id: string; kind: string; subject: string; outcome: string; explanation: string; seat: string; at: string }[];
  posted: { id: string; memo: string; amount_cents: number }[];
  waiting: {
    approver: string;
    name: string;
    title: string;
    total_cents: number;
    items: { id: string; kind: string; subject: string; amount_cents: number; since: string }[];
  }[];
  held: {
    decision: string;
    kind: string;
    subject: string;
    reason: string;
    lines: string[];
    amount_cents: number;
    since: string;
  }[];
  held_total_cents: number;
};

export type Message = {
  id: string;
  author: string;
  to: string;
  text: string;
  at: string;
  author_name: string;
  about: string | null; // the decision this message belongs to; its dollar figures trace there
};

export type ChatTurn = {
  action: { kind: string; approval_id?: string; message_id?: string; to?: string };
  clock: string;
  replies: Message[];
  timeline: TimelineItem[];
};

export type InjectEvent = { at: string; type: string; payload: Record<string, unknown>; summary?: string };

export type Parsed =
  | { events: InjectEvent[]; intents: unknown[]; source: string; clock: string }
  | { unmapped: string; reason: string | null; closest: string[]; source: string };

export type Case = {
  case_id: string;
  type: string;
  deal_id: string;
  rep_id: string;
  expected_outcome: string;
  expected_amount_cents: number;
  got_outcome: string;
  got_amount_cents: number;
  paid_cents: number;
  correct: boolean;
  note?: string;
};

export type Scorecard = {
  seed: number;
  reps: number;
  source: string;
  planted: number;
  correct: number;
  recall: number;
  false_flags: number;
  wrong_automatic_payouts: number;
  straight_through: number;
  escalations_per_100_events: number;
  replay_match: number;
  refusals: number;
  refusal_codes: Record<string, number>;
  misses: Case[];
  cases: Case[];
  events: number;
  decisions: number;
  lines: number;
  pushback: {
    held_invalid: number;
    invalid: number;
    conceded_valid: number;
    valid: number;
    results: { id: string; valid: boolean; got: string; right: boolean }[];
  } | null;
};

async function call<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, body === undefined ? undefined : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      // not JSON
    }
    throw new Error(String(detail));
  }
  return response.json() as Promise<T>;
}

export const api = {
  state: () => call<WorldState>("/state"),
  timeline: (since: number) => call<{ clock: string; items: TimelineItem[] }>(`/timeline?since=${since}`),
  trace: (id: string, asOf: string | null) =>
    call<Trace>(`/trace/${id.split("/").map(encodeURIComponent).join("/")}${asOf ? `?as_of=${encodeURIComponent(asOf)}` : ""}`),
  health: () => call<Health>("/ledger/health"),
  today: () => call<Today>("/today"),
  messages: (principal: string) => call<Message[]>(`/messages?principal=${encodeURIComponent(principal)}`),
  scorecard: (seed: number) => call<Scorecard>(`/scorecard?seed=${seed}`),
  chat: (as_principal: string, text: string) => call<ChatTurn>("/chat", { as_principal, text }),
  parse: (text: string) => call<Parsed>("/inject/parse", { text }),
  run: (events: InjectEvent[]) => call<{ clock: string; timeline: TimelineItem[] }>("/inject/run", { events }),
};
