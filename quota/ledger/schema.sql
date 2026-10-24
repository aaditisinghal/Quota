PRAGMA foreign_keys = ON;

-- ---------- facts (append-only, versioned) ----------
CREATE TABLE people (
  id TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL,
  state TEXT, manager_id TEXT, start_date TEXT, end_date TEXT,
  known_at TEXT NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY (id, version));

CREATE TABLE deals (
  id TEXT NOT NULL, version INTEGER NOT NULL, account TEXT NOT NULL,
  crm_amount_cents INTEGER NOT NULL, crm_close_date TEXT, stage TEXT NOT NULL,
  owner_id TEXT NOT NULL, splits_json TEXT NOT NULL,      -- [{"rep":"jordan.kim","bps":10000}]
  known_at TEXT NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY (id, version));

CREATE TABLE contracts (
  id TEXT NOT NULL, version INTEGER NOT NULL, deal_id TEXT NOT NULL,
  acv_cents INTEGER NOT NULL, term_months INTEGER NOT NULL,
  contract_date TEXT, countersigned_at TEXT, clauses_json TEXT NOT NULL,
  doc_ref TEXT NOT NULL, known_at TEXT NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY (id, version));

CREATE TABLE esign_events (
  id TEXT PRIMARY KEY, envelope_id TEXT NOT NULL, deal_id TEXT NOT NULL,
  event TEXT NOT NULL, at TEXT NOT NULL, known_at TEXT NOT NULL);

CREATE TABLE plans (
  id TEXT NOT NULL, version INTEGER NOT NULL, rep_id TEXT NOT NULL,
  effective_from TEXT NOT NULL, effective_to TEXT NOT NULL,
  config_json TEXT NOT NULL, signed_doc_ref TEXT, signed_at TEXT,
  known_at TEXT NOT NULL,
  PRIMARY KEY (id, version));

CREATE TABLE billing (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL,                 -- invoice | payment | refund
  deal_id TEXT NOT NULL, amount_cents INTEGER NOT NULL, at TEXT NOT NULL,
  external_ref TEXT, known_at TEXT NOT NULL);

CREATE TABLE messages (
  id TEXT PRIMARY KEY, channel TEXT NOT NULL, author TEXT NOT NULL, to_principal TEXT,
  text TEXT NOT NULL, claim_json TEXT, sent_at TEXT NOT NULL, known_at TEXT NOT NULL);

CREATE TABLE events (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, payload_json TEXT NOT NULL,
  release_at TEXT NOT NULL, created_by TEXT NOT NULL, known_at TEXT NOT NULL);

-- Phase 2: which actor has picked up which event (latest row per (event_id, actor) wins).
CREATE TABLE event_status (
  event_id TEXT NOT NULL REFERENCES events(id), status TEXT NOT NULL,   -- handled|ignored
  actor TEXT NOT NULL, known_at TEXT NOT NULL);

-- ---------- Quota's work ----------
CREATE TABLE decisions (
  id TEXT PRIMARY KEY, seat TEXT NOT NULL, kind TEXT NOT NULL, subject_id TEXT NOT NULL,
  inputs_json TEXT NOT NULL,          -- [{"table","id","version","sha256"}], non-empty
  resolutions_json TEXT NOT NULL,     -- per field: value, source, rule_id, disagreements
  precedent_ids_json TEXT NOT NULL,
  output_json TEXT NOT NULL,
  outcome TEXT NOT NULL,              -- posted|drafted|held|escalated|conceded|refused|superseded
  explanation TEXT NOT NULL,
  model TEXT, prompt_sha TEXT,        -- null in Phase 1
  known_at TEXT NOT NULL);

CREATE TABLE commission_lines (
  id TEXT PRIMARY KEY, rep_id TEXT NOT NULL, deal_id TEXT NOT NULL,
  booking_quarter TEXT NOT NULL, credit_cents INTEGER NOT NULL,
  base_portion_cents INTEGER NOT NULL, accel_portion_cents INTEGER NOT NULL,
  amount_cents INTEGER NOT NULL,      -- negative for clawbacks
  status TEXT NOT NULL,               -- draft|approved|paid|superseded|clawed_back
  decision_id TEXT NOT NULL REFERENCES decisions(id),
  corrects_id TEXT, known_at TEXT NOT NULL);

CREATE TABLE commission_line_status (
  line_id TEXT NOT NULL, status TEXT NOT NULL, actor TEXT NOT NULL, known_at TEXT NOT NULL);

CREATE TABLE precedents (
  id TEXT PRIMARY KEY, statement TEXT NOT NULL, scope_json TEXT NOT NULL,
  override_json TEXT NOT NULL, approvals_json TEXT NOT NULL,
  effective_from TEXT NOT NULL, expires_at TEXT NOT NULL, known_at TEXT NOT NULL);

CREATE TABLE approvals (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL,     -- plan_exception|journal_entry|payroll_file
  subject_id TEXT NOT NULL, amount_cents INTEGER NOT NULL,
  requested_by TEXT NOT NULL, approver TEXT NOT NULL,
  parent_id TEXT, known_at TEXT NOT NULL);

CREATE TABLE approval_decisions (
  approval_id TEXT NOT NULL REFERENCES approvals(id), decided_by TEXT NOT NULL,
  decision TEXT NOT NULL,                      -- approved|rejected
  result TEXT NOT NULL,                        -- final|over_limit_routed|rejected
  reason TEXT NOT NULL, known_at TEXT NOT NULL);

CREATE TABLE journal_entries (
  id TEXT PRIMARY KEY, period TEXT NOT NULL, effective_date TEXT NOT NULL,
  memo TEXT NOT NULL, decision_id TEXT REFERENCES decisions(id),
  source_ref TEXT,                    -- for system:billing entries
  corrects_je_id TEXT, created_by TEXT NOT NULL, known_at TEXT NOT NULL);

CREATE TABLE je_lines (
  je_id TEXT NOT NULL REFERENCES journal_entries(id), line_no INTEGER NOT NULL,
  account TEXT NOT NULL, debit_cents INTEGER NOT NULL DEFAULT 0, credit_cents INTEGER NOT NULL DEFAULT 0,
  subledger_ref TEXT, PRIMARY KEY (je_id, line_no));

CREATE TABLE je_status (
  je_id TEXT NOT NULL, status TEXT NOT NULL,   -- draft|pending_approval|posted
  actor TEXT NOT NULL, known_at TEXT NOT NULL);

CREATE TABLE amortization_schedules (
  id TEXT PRIMARY KEY, line_id TEXT NOT NULL, total_cents INTEGER NOT NULL,
  months INTEGER NOT NULL, start_period TEXT NOT NULL, schedule_json TEXT NOT NULL,
  status TEXT NOT NULL, known_at TEXT NOT NULL);   -- active|cancelled (cancel = new row, same id suffix -c)

CREATE TABLE payroll_files (
  id TEXT PRIMARY KEY, period TEXT NOT NULL, lines_json TEXT NOT NULL,
  total_cents INTEGER NOT NULL, known_at TEXT NOT NULL);

CREATE TABLE payroll_status (
  file_id TEXT NOT NULL, status TEXT NOT NULL, actor TEXT NOT NULL, known_at TEXT NOT NULL);

CREATE TABLE periods (
  period TEXT NOT NULL, status TEXT NOT NULL, actor TEXT NOT NULL, known_at TEXT NOT NULL);
  -- latest row per period wins; initial rows "open" for 2026-01..2026-12

CREATE TABLE refusals (
  id TEXT PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, subject_id TEXT,
  reason_codes_json TEXT NOT NULL, detail TEXT NOT NULL, known_at TEXT NOT NULL);

CREATE TABLE chain (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, ref_id TEXT NOT NULL,
  payload_sha TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL, known_at TEXT NOT NULL);
