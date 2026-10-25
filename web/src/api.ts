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

export type Statement = {
  rep: string;
  name: string;
  as_of: string;
  quota_cents: number | null;
  quarters: {
    quarter: string;
    credit_cents: number;
    attainment_bps: number | null;
    earned_cents: number;
    paid_cents: number;
    held_cents: number;
    lines: { id: string; deal: string; amount_cents: number; status: string; hold: { decision: string; reason: string } | null }[];
  }[];
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

// Scenarios and presenter mode (NEXT_TASKS R4, R7.3). Servers without them answer 404 or 405.
export type Scenario = { name: string; phrase: string; checkpoint: string; expect: unknown };

// A run's `result` is that scenario's own facts (lines, refusals, entries, a reply); `expect` says what it should show.
export type ScenarioRun = Scenario & {
  clock: string;
  timeline: TimelineItem[];
  result: { ok?: boolean; summary?: string; reply?: string; tab?: string; [field: string]: unknown };
};

// Step 0 is the start; each later step restores a named checkpoint of the scripted demo.
export type DemoStep = { n: number; narration: string; checkpoint?: string; tab?: string };

export type DemoStepRun = DemoStep & { clock: string; timeline: TimelineItem[] };

/** An API error with its HTTP status, so a screen can tell "not on this server" from a failure. */
export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
  get missing(): boolean {
    return this.status === 404 || this.status === 405;
  }
}

export function isMissing(e: unknown): boolean {
  return e instanceof ApiError && e.missing;
}

// Intake of messy exports (NEXT_TASKS R6): only on a server started with --from-inbox; others answer 404.
export type Mapping = {
  source_column: string;
  canonical_field: string;
  transform: string;
  confidence: number;
  proposed_by: string;
  parse_rate?: number;
  type_ok?: boolean;
  problems?: string[];
  status: string;
};

export type ReviewItem = {
  id: string;
  kind: string;
  routed_to: string;
  question: string;
  file?: string;
  row?: number;
  [detail: string]: unknown;
};

export type Finding = {
  id: string;
  outcome: string;
  routed_to: string;
  file: string;
  row: number;
  rep: string;
  deal: string;
  paid_cents: number;
  correct_cents: number;
  overpaid_cents: number;
  class: string;
  text: string;
};

export type Intake = {
  inbox: string;
  files: { file: string; kind: string | null; rows: number; loaded?: boolean | null; mapping_source?: string | null; mappings: Mapping[] }[];
  loaded: Record<string, number>;
  review_items: ReviewItem[];
  log: string[];
  plans: Record<string, { file: string; terms?: Record<string, { value: unknown; quote: string; section: number; read_by?: string }> }>;
  backtest: {
    quarter: string;
    rows: number;
    matched: number;
    errors: number;
    unexplained: number;
    overpaid_cents: number;
    headline: string;
    findings: Finding[];
  };
};

// The CFO demo flow (DEMO_FLOW_TASKS D1-D5). Every figure is the server's; the screens only lay it out.
export type Band = "cleared" | "note" | "held";

export type Factor = { name: string; value: number; reason: string };

export type Confidence = {
  line: string;
  deal?: string;
  decision?: string;
  score: number;
  band: Band;
  band_text?: string;
  factors: Factor[];
  stored?: { score: number; band: Band; factors: Factor[] } | Record<string, unknown> | null;
  hold?: { decision: string; kind: string; reason: string } | null;
};

export type Calibration = {
  line: string;
  deal?: string;
  rep?: string;
  decision?: string;
  evidence: { source: string; field?: string; value: unknown; agrees: boolean | null; label?: string; ref?: string | null }[];
  rules: { field: string; rule: string; chosen: unknown; alternatives?: unknown[] }[];
  amount: {
    total?: number;
    total_cents?: number;
    credit_cents?: number;
    base_portion?: number;
    accel_portion?: number;
    base_portion_cents?: number;
    accel_portion_cents?: number;
    tier?: string;
    quarter?: string;
    prior_credit_cents?: number;
    rate?: number | string;
    [k: string]: unknown;
  };
  confidence: Confidence;
  outcome: {
    status: string;
    line_status?: string;
    files?: unknown;
    text: string;
    asked?: (Partial<NeedsYouItem> & { person: string; question: string; item?: string }) | null;
    approval?: string | null;
  };
};

export type Source = { kind: string; label: string; count: number; via?: string; last_sync?: string | null; detail?: string };

export type SourceRows = {
  kind: string;
  columns: string[];
  total?: number;
  // highlight: the extracted columns, or for a message the quoted spans of its text
  rows: ({ highlight?: string[]; ref?: string } & Record<string, unknown>)[];
};

export type RepRow = {
  rep: string;
  name: string;
  state?: string;
  left?: string | null;
  pipeline: { total_cents: number; by_stage: Record<string, number>; deals: number; simulated: boolean };
  booked_quarter_cents: number;
  quarter: string;
  attainment_bps: number | null;
  pending_cents: number;
  lowest_confidence: number | null;
  status: string; // Held | Note | Cleared
};

export type Recipient = {
  kind: string; // rep, manager_override (later: se_pool, partner, account_manager)
  who: string;
  name?: string;
  share_bps: number;
  rate_bps?: number;
  paid_via?: string;
  note?: string;
  line: string | null;
  confidence: Confidence | null;
  amount_cents?: number;
  transfer?: string | null;
};

export type ContractChild = {
  id: string;
  version: number;
  signed_at: string;
  acv_cents: number;
  incremental_lines: string[] | { id: string; amount_cents: number }[];
  incremental_commission_cents: number;
  recipients?: Recipient[];
};

export type ContractCard = {
  id: string; // the deal id
  customer: string;
  acv_cents: number;
  term_months: number;
  billing?: string;
  ramp?: string | null;
  clauses?: Record<string, unknown>;
  countersigned_at?: string | null;
  countersigned_local?: string | null;
  paid?: boolean;
  overdue?: boolean;
  lines: { id: string; rep: string; amount_cents: number; quarter: string; status: string }[];
  viewer?: string | null;
  recipients: Recipient[];
  children: ContractChild[];
  parent: string | null;
  split_bar: { who: string; share_bps: number; line: string | null }[];
};

export type OpenDeal = {
  id: string;
  account: string;
  stage: string;
  amount_cents: number;
  expected_close?: string;
  forecast_commission_cents: number;
  earned: false;
  simulated: true;
};

export type RepDetail = {
  rep: string;
  name?: string;
  title?: string;
  state?: string;
  manager?: string;
  left?: string | null;
  pipeline: { simulated: boolean; stages: string[]; deals: OpenDeal[] };
  contracts: ContractCard[];
};

export type NeedsYouItem = {
  id: string;
  kind: string;
  person?: string;
  question: string;
  impact_cents: number;
  deals?: string[];
  evidence?: { decision?: string; text?: string; ref?: string }[];
  chain?: { at: string; text: string }[];
  answer?: "approve_reject" | "reply";
};

export type NeedsYou = { count: number; people: { person: string; name?: string; title: string; items: NeedsYouItem[] }[] };

export type Home = {
  clock?: string;
  period?: string;
  pending_cents: number;
  pending_lines?: number;
  cleared_pct: number | null;
  held_cents: number;
  held_by_reason: Record<string, number>;
  needs_you: number;
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
    throw new ApiError(String(detail), response.status);
  }
  return response.json() as Promise<T>;
}

const seg = encodeURIComponent;

export const api = {
  home: () => call<Home>("/home"),
  sources: () => call<Source[]>("/sources"),
  sourceRows: (kind: string, q: string) => call<SourceRows>(`/sources/${seg(kind)}/rows${q ? `?q=${seg(q)}` : ""}`),
  reps: () => call<RepRow[]>("/reps"),
  rep: (id: string) => call<RepDetail>(`/reps/${seg(id)}`),
  contract: (id: string, viewer?: string) => call<ContractCard>(`/contracts/${seg(id)}${viewer ? `?viewer=${seg(viewer)}` : ""}`),
  confidence: (line: string) => call<Confidence>(`/lines/${seg(line)}/confidence`),
  calibration: (line: string) => call<Calibration>(`/lines/${seg(line)}/calibration`),
  needsYou: () => call<NeedsYou>("/needs-you"),
  answer: (id: string, as_principal: string, decision: "approve" | "reject" | "reply", text: string) =>
    call<{ id: string; ok: boolean; result?: unknown; refused?: unknown }>(`/needs-you/${seg(id)}/answer`, { as_principal, decision, text }),
  state: () => call<WorldState>("/state"),
  timeline: (since: number) => call<{ clock: string; items: TimelineItem[] }>(`/timeline?since=${since}`),
  trace: (id: string, asOf: string | null) =>
    call<Trace>(`/trace/${id.split("/").map(encodeURIComponent).join("/")}${asOf ? `?as_of=${encodeURIComponent(asOf)}` : ""}`),
  health: () => call<Health>("/ledger/health"),
  today: () => call<Today>("/today"),
  messages: (principal: string) => call<Message[]>(`/messages?principal=${encodeURIComponent(principal)}`),
  scorecard: (seed: number) => call<Scorecard>(`/scorecard?seed=${seed}`),
  statement: (rep: string, viewer: string) =>
    call<Statement>(`/statement/${encodeURIComponent(rep)}?as_principal=${encodeURIComponent(viewer)}`),
  chat: (as_principal: string, text: string) => call<ChatTurn>("/chat", { as_principal, text }),
  parse: (text: string) => call<Parsed>("/inject/parse", { text }),
  scenarios: () => call<Scenario[]>("/scenarios"),
  runScenario: (name: string) => call<ScenarioRun>(`/scenarios/${encodeURIComponent(name)}/run`, {}),
  reset: () => call<{ clock: string }>("/demo/reset", {}),
  steps: () => call<DemoStep[]>("/demo/steps"),
  step: (n: number) => call<DemoStepRun>("/demo/step", { n }),
  intake: () => call<Intake>("/intake"),
  review: (id: string, resolution: Record<string, unknown>) =>
    call<Intake & { resolved: string }>(`/review/${encodeURIComponent(id)}`, { resolution }),
  run: (events: InjectEvent[]) => call<{ clock: string; timeline: TimelineItem[] }>("/inject/run", { events }),
};
