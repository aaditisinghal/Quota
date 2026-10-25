# Hyphenate Track — Commissions Desk Build Spec

Oct 24, 2026 · @krishna

## The pitch

Build Quota (working name): the commissions desk of a 60-rep B2B SaaS company, run autonomously, with the coworkers who argue with it built into the demo.

**Seats line (submit verbatim):** Fills the commissions analyst and deferred-commission accountant seats at a 60-rep SaaS company; drafts every payout and journal entry, approves none.

**What it does.** Every month it calculates each rep's payout from their signed plan, capitalizes and amortizes the cost under ASC 340-40, and catches what the CRM gets wrong. When a rep pushes back, it defends the number with evidence. When the evidence says it was wrong, it concedes and posts the correction in the open period.

**The bet.** Past winners proved the baseline: the LLM reads, code counts, a human approves. This brief adds two things almost nobody delivered last round: recovering from a mistake and defending a decision under questioning. Commissions puts a live adversary in the demo (the rep), and one churn event cascades through eight numbers that must still agree.

**Fit with Hyphenate.** Commissions isn't one of Hyphenate's five product areas (order-to-cash, treasury, procure-to-pay, GL, reporting). Pitch it as the twin of revenue recognition: ASC 340-40 sits beside ASC 606, and the churn cascade visibly moves revenue, deferred revenue and cash. Ask their booth Saturday morning what breaks most often in commission accounting, and add the answer as a planted error.

**Assumptions.** Hacking started Friday 6 pm. If it runs 36 hours, submission is Sunday around 6 am, which leaves about 26 working hours from 2 am Saturday. Confirm the real deadline and adjust the build plan.

## Scope

Quota owns one object: **the commission line**, from signed contract to paid paycheck to booked expense. It reads from Sales Ops and Revenue Accounting, hands finished drafts to Payroll and the general ledger, and never edits another team's system.

**Pitch line:** Quota owns every commission from contract to paycheck to ledger. It reads Sales and Revenue data, hands Payroll and the GL finished drafts, and asks a human only when policy says it must.

### What it contributes to each function

|  | Sales Ops | Revenue Accounting | Payroll |
| --- | --- | --- | --- |
| **Reads** | CRM deals and field history, signed comp plans, quotas, territories | Contracts, invoices, payments, refunds, churn, contract changes | Employee records: start and end dates, state, status |
| **Produces** | Rep statements; attainment by quarter; dispute answers with evidence; data-quality flags (wrong amount, backdated close date, splits not adding to 100%) | Draft entries to capitalize, amortize and write off commission cost (ASC 340-40); a commission subledger that reconciles to the GL; month-end accrual for unapproved commissions | One approved payout file per period with gross amount per rep and the line items behind it; same-day payout requests for terminated reps; recovery of overpaid commissions from future commission advances |
| **Never does** | Design plans, set quotas, assign territories, edit the CRM | Book revenue, decide revenue recognition, close the books | Withhold taxes, run payroll, change base pay, release money |
| **What it saves** | Dispute handling and statement questions | Commission schedules and month-end reconciliation | Last-minute corrections and final-pay penalties |

The three connect through Quota. A churn arrives from Revenue Accounting; Quota sends a recovery draft to Payroll, a write-off entry to the GL, and an attainment correction to Sales Ops. Quota is the only place all three effects are calculated together and kept in agreement.

### The escalation contract

Quota escalates in exactly these eight cases and does everything else without asking. Each escalation names the trigger, the dollar impact and the evidence. Every answer is saved as a policy with its scope, so the same question isn't asked twice.

| # | Trigger | Goes to |
| --- | --- | --- |
| 1 | Sources disagree above $500 or 2% of the payout, and no saved policy covers it | Plan owner (VP Sales Ops) |
| 2 | Someone's authority is needed: an exception, a split, a reading of the plan | Approver within their dollar limit, then the next one up |
| 3 | Unknown situation: a clause, field or currency that matches no rule (`NO_RULE`) | Plan owner |
| 4 | Legally sensitive: termination, recovering money in California, leave of absence, unsigned plan | Controller (and HR for leave) |
| 5 | Integrity signal: post-lock backdating, the approver benefits from the decision, repeated exceptions for one rep | Controller |
| 6 | Correction to a locked period above materiality | Controller |
| 7 | Any entry, payout file or period lock that policy requires a person to sign | Controller (Payroll releases payment) |
| 8 | Its own checks fail: ledger out of balance, replay mismatch, extraction can't be verified | Controller; the affected lines are held |

### Deliberately out of scope

- **Plan design:** Sales Ops strategy.
- **Revenue recognition:** owned by the revenue system; Quota only consumes its events.
- **Running payroll and tax withholding:** the payroll provider.
- **Forecasting commission spend:** FP\&A; Quota can export actuals to it.

This boundary is the answer to "isn't this CaptivateIQ?" Those tools calculate payouts for a comp admin. Quota does the work between the calculation and the books, and stops at clearly defined handoffs.

## What the judges look for

Every feature below exists to answer one of the four judging questions or one of the brief's five hard parts. If a feature doesn't map to a row here, cut it.

### The four questions

| Judge question | What they will check | What answers it | What they see on screen |
| --- | --- | --- | --- |
| Is it real enough? | Realistic data, a messy problem, proof it works | A 6-month seeded company world; Stripe test mode as a real billing system; 16 planted error types with an answer key | Scorecard in the README and live: recall, precision, zero wrong automatic payouts |
| Would you hire it? | Which seats; limited access; a clean record; knowing when to ask | Two seat identities with separate permissions; SoD enforced in the write path; approval limits by person and amount; escalations only above materiality | Permission log showing the agent's own approval attempt rejected; escalations per 100 events |
| Did we see something new? | A company, problem or approach not seen made autonomous | Commissions plus ASC 340-40; adversarial coworkers; as-of explanations from a bitemporal ledger; judge-driven scenario injector | The rep dispute and the as-of trace view |
| The moment | Catches what others missed, recovers, defends when questioned | All three in one 3-minute script (see Demo script) | Rep dispute held, VP exception routed past its limit, churn cascade corrected in the open period |

### The five hard parts in the brief

| Hard part (brief's words) | How Quota handles it | Demo proof |
| --- | --- | --- |
| Sources that disagree | A ranked source of truth per field; every decision names the winning source and the rule that made it win | CRM says $150k, contract says $120k: pays on $120k and says why |
| Time | Amortization over months; clawback windows; pay-on-collection holds; every fact carries effective date and known-at date | March booking changes the June payout after churn; backdated CRM edit caught by its known-at timestamp |
| Working with others | Busy approvers get one daily digest; the Controller can say no; a Sales Ops bot hands over a territory move half done | Controller rejects a payout; Quota holds, records the reason, never resubmits unchanged |
| Limits | Each seat has a capability list; requester can never approve; approval limits per person | VP Sales Ops approves an exception, but the amount exceeds her limit, so it also goes to the Controller |
| Explainable and accountable | Click any number to see the entry, rule or precedent, source documents and approver, as of the decision date | Trace of a 4-month-old payout replayed with only what was known then |

### Submission requirements, mapped

- **Demo video opens with the moment:** first 20 seconds = the rep dispute.
- **Live demo running for real, with a judge's own scenario:** the scenario injector (see Event grammar).
- **One line on the seats:** the seats line above.
- **Proof, real vs simulated, and how you know:** the Proof and scorecard section, copied into the README top.

### Signals from the workshop and site

- The workshop title is Beyond Agentic Harnesses. Show a system with a ledger, controls and a clock, not a chat wrapper around tools.
- Hyphenate's site sells four things: runs on top of your stack, learns your process, escalates to the right person, audit trail on every entry. Use the same words in the pitch so judges map you to their product instantly.

## Winning and unique aspects

Hyphenate is Maximor renamed (its site's press links cover Maximor's $9M raise), so the HackMIT Maximor winners are this team's past picks. Match their baseline, then beat them on recover and defend.

### Against past winners

| Past winner | What won it | What Quota adds beyond it |
| --- | --- | --- |
| VistaAI (1st) | Company type nobody else picked; founder domain credibility; provenance to file and row | Provenance to file and row **as of the decision date**, plus a person and an authority limit on every override |
| CFO-Sabbatical (2nd) | Breadth: 15 agents across AP, AR, recon, forecast; an observability view | Opposite bet, as the new brief asks: two seats, done deeply. Keep the observability idea as the trace view |
| Money Maxer (Ramp 3rd, Maximor 4th) | Scoped memory of human answers; deterministic verifier; adversarial review of its own controls; hard benchmark numbers | Precedents also record who had authority and up to what amount; a pushback test that scores whether the system holds or concedes correctly |
| TrueUp (5th) | Time-gated data with no future leakage; four obligation types; replay-tested rules | Full bitemporal ledger, so the past can be replayed and late or backdated edits are caught |

### Against likely CalHacks competitors

- **Vendor bank-detail fraud in AP.** It is in the brief, so many teams will pick it. Quota's world is different.
- **AR collections and short payments.** Already won by Money Maxer.
- **Themed companies (F1, Mars colony, festival, coffee chain).** The brief and slides name these; expect repeats and copies of the slide examples. Quota's novelty is the problem, not a costume.
- **The Learns, Runs, Escalates, Improves loop.** That is Hyphenate's own slide framing; dozens of teams will mirror it. Quota has it, but leads with the dispute.

### Unique aspects to say out loud

1. **Adversarial coworkers.** Reps dispute their pay; that is the normal month in commissions. No other finance seat has a live adversary.
2. **Doesn't cave.** A pushback test: 20 scripted disputes, 10 valid and 10 not. Score = holds when right, concedes when wrong. This is a new metric for this track.
3. **As-of explanations.** Every number can be replayed with only what was known when it was decided. Accountable no matter how long ago, literally.
4. **Backdating detection.** A rep edits a CRM close date after the quarter locks. The known-at timestamp exposes it.
5. **Authority-scoped exceptions.** An override records who approved, within what limit, for what scope, until when. Over the limit routes upward automatically.
6. **One event, eight numbers.** A churn ripples through revenue, deferred revenue, cash, clawback, the deferred commission asset, quota attainment, accelerator tier and the next payout, with every check green.
7. **Judge scenario injector.** A judge types a scenario; it becomes typed events and runs live.
8. **Written-plan rule.** California requires signed written commission plans (Labor Code 2751), so Quota won't enforce a clawback that isn't in that rep's signed plan version. Verified: 2751 requires a signed written contract stating how commissions are computed and paid; refusing unsigned terms is Fernhill's policy reading of it, not a statement of case law.

## The company and its world

Fernhill is a fictional B2B analytics SaaS: about $40M ARR, 60 quota-carrying reps in 4 regions, selling annual and multi-year contracts with ramps. The simulation runs January to June 2026, with about 900 deals.

### People and authority

Approval limits are per person and enforced in code. Model-played people use a fixed persona prompt and can only act through the same approval API as real users.

| Person | Role | Can approve | Limit | Cannot |
| --- | --- | --- | --- | --- |
| Priya Nair | Controller | Journal entries, payout runs, period locks, exceptions over the VP's limit | Unlimited | Initiate a payout |
| Marcus Lee | VP Sales Ops | Plan interpretation and per-deal exceptions | $5,000 per rep per quarter | Release payment; post entries |
| Dana Ortiz | Payroll manager | Releases the approved commission payroll file | Must match the approved total exactly | Change any amount |
| Sales Ops bot | Automated colleague | None; reassigns territories in the CRM | n/a | Approve anything |
| Reps (60) | AEs, AMs, SDRs | None; can dispute their own lines | n/a | See other reps' pay |
| Auditor (model-played) | External | None; requests evidence | n/a | Change anything |

Named reps for the demo: **Jordan Kim** (Acme deal, accelerator dispute), **Alex Chen** (split conflict), **Taylor Brooks** (terminated mid-quarter, California), **Riley Shah** (backdated close date).

### Quota's two seats

| Seat | Owns | Reads | Writes (drafts only) |
| --- | --- | --- | --- |
| Commissions analyst | Monthly payout calculation, disputes, holds, clawbacks | CRM, e-signature log, contracts, plans, Stripe, HR records, Slack | Commission lines, payout draft, dispute replies, approval requests |
| Deferred-commission accountant | Capitalization, amortization, write-offs, reconciliation to the GL | Commission lines, contracts, policy | Journal entry drafts, reconciliation reports |

The seats hand work to each other through the same queue a human team would use. Neither can approve, release payment or lock a period.

### Systems

| System | Real or simulated | Built from |
| --- | --- | --- |
| Billing (customers, invoices, payments, refunds) | Real: Stripe test mode | Stripe API, test clock to move time |
| CRM (opportunities, owners, splits, stages) | Simulated | SQLite with Salesforce-shaped fields |
| E-signature log (sent, viewed, signed, countersigned) | Simulated | JSON envelope events with timestamps |
| Contracts | Simulated | Generated PDFs (ACV, term, ramp, opt-out) |
| Comp plans | Simulated | YAML config per rep and version, plus a signed PDF |
| HR records | Simulated | CSV: start, end, state, territory history |
| Slack | Real workspace if time allows, else in-app chat | Slack Bolt app or web chat pane |
| General ledger | Built by us | Bitemporal ledger (see Architecture) |
| Payroll export | Simulated | CSV the payroll manager releases |

### Chart of accounts used

| Account | Name | Type |
| --- | --- | --- |
| 1000 | Cash | Asset |
| 1200 | Accounts receivable | Asset |
| 1290 | Due from employees (clawbacks) | Asset |
| 1350 | Deferred commissions, current | Asset |
| 1450 | Deferred commissions, noncurrent | Asset |
| 2150 | Commissions payable | Liability |
| 2400 | Deferred revenue | Liability |
| 4000 | Subscription revenue | Revenue |
| 6110 | Commission expense, amortization | Expense |
| 6115 | Commission expense, not capitalized | Expense |

## Architecture

Two seats share one write path and one ledger; nothing else can write.

&#91;embedded content: Quota architecture · two seats, one write path, one ledger\]

Events reach the seats only when the clock releases them. Every draft passes the write path, which posts to the ledger or sends an approval request to a person.

### Rules the code enforces

- Models output typed facts with a page or message reference, and draft explanations. They never output a dollar amount that gets posted.
- Only the write path holds a database write handle. The seats hold read-only tool credentials scoped to their sources.
- The seats hand work to each other through queues and the decision log, the same way a human team would.
- Every read is recorded with its version and hash, so any decision can be replayed exactly.

### Suggested stack

Python with FastAPI and SQLite; Stripe Python SDK in test mode with test clocks; any model with structured outputs; a React front end. Use whatever the team is fastest in.

## Data schema

Every business fact row carries two times: `effective_at` (when it is true in the business) and `known_at` (when Quota learned it). Rows are never updated; a change is a new row that supersedes the old one. Entries are hash-chained. SQLite is enough; Postgres if you already have it.

```sql
-- Source facts (append-only; a change = new row with supersedes_id)
CREATE TABLE reps (id TEXT, version INT, name TEXT, role TEXT, region TEXT, state TEXT,
  start_date DATE, end_date DATE, effective_at TS, known_at TS, supersedes_id TEXT, source TEXT);
CREATE TABLE deals (id TEXT, version INT, account TEXT, crm_amount NUMERIC, close_date DATE,
  stage TEXT, owner_rep_id TEXT, splits JSON, effective_at TS, known_at TS, supersedes_id TEXT, source TEXT);
CREATE TABLE contracts (id TEXT, deal_id TEXT, acv NUMERIC, tcv NUMERIC, term_months INT,
  ramp JSON, opt_out JSON, countersigned_at TS, doc_id TEXT, known_at TS);
CREATE TABLE plans (id TEXT, rep_id TEXT, version INT, effective_from DATE, effective_to DATE,
  config JSON, signed_doc_id TEXT, signed_at TS, known_at TS);
CREATE TABLE billing (stripe_id TEXT, kind TEXT, customer TEXT, deal_id TEXT, amount NUMERIC,
  status TEXT, effective_at TS, known_at TS);           -- invoices, payments, refunds
CREATE TABLE messages (id TEXT, channel TEXT, author TEXT, text TEXT, sent_at TS, known_at TS);
CREATE TABLE documents (id TEXT, kind TEXT, uri TEXT, sha256 TEXT, known_at TS);

-- Quota's work
CREATE TABLE decisions (id TEXT, seat TEXT, kind TEXT, subject_id TEXT,
  inputs JSON,          -- [{table, id, version, sha256}] exactly what was read
  source_ranking JSON,  -- which source won per field, and the rule id
  rule_ids JSON, precedent_ids JSON, output JSON, materiality NUMERIC,
  outcome TEXT,         -- posted | escalated | held | rejected | superseded
  model TEXT, prompt_sha TEXT, decided_known_at TS, prev_hash TEXT, hash TEXT);
CREATE TABLE commission_lines (id TEXT, rep_id TEXT, deal_id TEXT, period TEXT, base NUMERIC,
  rate NUMERIC, tier TEXT, amount NUMERIC, status TEXT, decision_id TEXT, corrects_id TEXT);
  -- status: draft | held | awaiting_approval | approved | paid | clawed_back
CREATE TABLE precedents (id TEXT, statement TEXT, scope JSON, authority_person TEXT,
  authority_limit NUMERIC, effective_from DATE, expires_at DATE, source_message_id TEXT, approved_by JSON);
CREATE TABLE approvals (id TEXT, decision_id TEXT, requested_by_seat TEXT, approver TEXT,
  status TEXT, reason TEXT, at TS);
CREATE TABLE journal_entries (id TEXT, period TEXT, effective_date DATE, memo TEXT,
  decision_id TEXT, corrects_je_id TEXT, created_known_at TS, prev_hash TEXT, hash TEXT);
CREATE TABLE je_lines (je_id TEXT, account TEXT, debit NUMERIC, credit NUMERIC, subledger_ref TEXT);
CREATE TABLE periods (period TEXT, status TEXT, locked_by TEXT, locked_at TS);
CREATE TABLE capabilities (principal TEXT, capability TEXT);   -- seat or person
```

### Invariants checked on every write

A write that breaks any of these is refused and logged; the refusal itself is a demo artifact.

1. Every journal entry balances: total debits = total credits.
2. No entry, line or status change lands in a locked period. Corrections post in the open period with `corrects_je_id`.
3. Commission subledger total = GL 2150 + payments made; deferred commissions subledger = GL 1350 + 1450.
4. Every commission line has a decision; every decision lists its inputs with hashes.
5. Requester is never the approver; the approver's limit covers the amount, or a second approver is attached.
6. Payroll file total = sum of approved lines, exactly.
7. A rep's split credits on a deal sum to 100%.
8. Hash chain verifies end to end.

### The as-of query

Replaying a decision means reading every table with `known_at <= decided_known_at` and taking the latest version per id. Run the calculator again on that view; the result must equal the stored output. That replay is the cross-examination view.

## Comp plan config and accounting rules

Plans are config, not prompts: the calculator reads YAML and never asks a model for a dollar amount. The model only reads documents and messages and turns them into typed facts.

### Plan config (one file per rep per version)

```yaml
plan_id: AE-2026-H1
version: 3
rep: jordan.kim
effective: {from: 2026-01-01, to: 2026-06-30}
signed_doc: plans/jordan.kim/AE-2026-H1-v3.pdf
quota: {period: quarter, amount: 300000}
booking_date: countersignature        # section 4
commissionable_base: year1_acv         # multi-year deals: year 1 only
rates:
  - {tier: base, up_to_attainment: 1.00, rate: 0.10}
  - {tier: accelerator, above_attainment: 1.00, rate: 0.15}
splits: {must_sum_to: 1.00, approver: vp_sales_ops}
spiffs: []                             # only via a signed plan amendment
pay_on: collection                     # paid the month after first customer payment
collection_hold_days: 90
clawback: {window_months: 6, triggers: [churn, termination_for_convenience, full_refund], basis: full}
termination: {pay_if_booked_and_collected_before: end_date}
manager_override: {rate: 0.02, manager: morgan.diaz}
```

### Source of truth per field

A written exception from someone with authority, within their limit, creates a precedent that outranks the plan for its scope only.

| Field | Ranking, highest first | Never trusted alone |
| --- | --- | --- |
| Commissionable amount | Signed contract, executed amendment, CRM | CRM amount |
| Booking date | Countersignature timestamp, contract date field, CRM close date | CRM close date |
| Owner and splits | VP-approved split record, CRM owner field | Slack claims |
| Plan terms | Signed plan version effective on booking date | Slack or email promises |
| Payment status | Stripe | CRM |
| Employment status | HR records | CRM user flag |

### Accounting policy

- **Capitalize** commissions that are incremental to winning a contract, including manager overrides. Expense SDR meeting bonuses and anything not tied to a signed contract (6115).
- **Amortize** straight-line over the benefit period. Fernhill's renewal commissions are commensurate, so the benefit period is the contract term. A term of 12 months or less is expensed when incurred (ASC 340-40 practical expedient).
- **Split** the asset: the next 12 months of amortization sits in 1350, the rest in 1450, reclassed monthly.
- **On churn**, write off the unamortized asset. If the signed plan has a clawback, recover from the rep: reverse the amortized expense and book a receivable or net it against the next payout. If it doesn't (Taylor's case), expense the remainder and recover nothing.

### Worked example: Jordan's Acme deal

Acme: $120,000 ACV, 36-month term, invoiced annually upfront, termination-for-convenience clause in the first 90 days with pro-rata refund. Jordan had booked exactly $300,000 (100% of quota) in Q1 before Acme.

- If Acme counts in Q1 (the VP's exception), it is entirely above quota: 15% x $120,000 = **$18,000**.
- If it counts in Q2 (countersigned April 2): 10% x $120,000 = **$12,000**.
- The difference, **$6,000**, exceeds the VP's $5,000 limit, so the exception also needs the Controller.

| Date | Event | Debit | Credit | Amount |
| --- | --- | --- | --- | --- |
| Apr 2 | Invoice issued | 1200 AR | 2400 Deferred revenue | $120,000 |
| Apr 15 | Customer pays | 1000 Cash | 1200 AR | $120,000 |
| Apr 30 | Commission earned (exception approved) | 1350/1450 Deferred commissions | 2150 Commissions payable | $18,000 |
| Apr 30 | Manager override 2% | 1350/1450 Deferred commissions | 2150 Commissions payable | $2,400 |
| Apr, May, Jun | Revenue, monthly | 2400 Deferred revenue | 4000 Revenue | $10,000 each |
| Apr, May, Jun | Amortization, Jordan, monthly ($18,000 / 36) | 6110 Commission expense | 1350 Deferred commissions | $500 each |
| Jun 28 | Acme terminates; pro-rata refund | 2400 Deferred revenue | 1000 Cash | $90,000 |
| Jun 30 | Clawback, Jordan (signed clawback clause) | 1290 Due from employees $18,000 | 1350/1450 $16,500; 6110 $1,500 | $18,000 |

The manager override is clawed back the same way. The 13-week forecast drops $10,000 a month of Acme revenue. Jordan's July payout nets the $18,000 receivable against new commissions, and anything left stays in 1290 with a note to Payroll.

## Event grammar and scenario injector

The whole world, seeded or judge-injected, is a stream of typed events. The simulator only releases an event once the clock passes its `known_at`, so the agent never sees the future.

### Event types

| Event | Required fields | Typical effect |
| --- | --- | --- |
| `deal.created` / `deal.updated` | deal\_id, fields changed, actor | New CRM version; backdated edits keep their real known\_at |
| `contract.countersigned` | deal\_id, timestamp, acv, term, ramp, clauses | Sets booking date and commissionable base |
| `contract.amended` | deal\_id, new terms, signed\_at | Recalculate; correct prior lines in the open period |
| `invoice.issued` / `payment.received` / `refund.issued` | stripe\_id, deal\_id, amount, date | Created in Stripe test mode, mirrored in |
| `payment.overdue` | deal\_id, days | Collection hold at 90 days |
| `customer.churned` | deal\_id, effective\_date, reason, refund | Clawback check, asset write-off, forecast update |
| `rep.terminated` | rep\_id, end\_date, state | Termination rules from that rep's signed plan |
| `rep.territory_changed` | rep\_id, accounts, effective\_date, actor | Split credit check; may be half done |
| `plan.amended` | rep\_id, version, changes, signed\_at | New plan version, effective from its date |
| `message.posted` | channel, author, text | Parsed into a claim, never into a fact |
| `approval.decided` | approval\_id, approver, decision, reason | Post, hold or revise |
| `period.lock_requested` | period | Runs close checks, asks the Controller |
| `clock.advance` | to | Releases queued events |

### Scenario injector

1. The judge types a sentence. A model call with a strict JSON schema (enums for event types, required fields) turns it into one or more events.
2. The UI shows the parsed events and the world objects they touch before anything runs. The judge clicks Run or edits a field.
3. The events enter the queue with known\_at = now; the clock advances; the judge watches the agent work in the inbox and trace view.
4. If a sentence can't be mapped, the injector says which part it couldn't map and offers the closest event. It never improvises a new event type.

### Judge phrases to rehearse

| Judge says | Parsed into |
| --- | --- |
| Acme churns in June and wants its money back | `customer.churned` + `refund.issued` |
| Jordan says his manager promised him a SPIF | `message.posted` from Jordan claiming a SPIF from Morgan |
| The CFO says pay Taylor anyway | `message.posted` from the CFO + `approval.decided` attempt |
| Change Alex's split to 70/40 | `deal.updated` with splits summing to 110% |
| Riley moves the close date back to March 30 | `deal.updated` with effective close date in a locked period |
| Acme never pays | `payment.overdue` 91 days |
| The Controller is on vacation | Approver unavailable; escalation path to the backup approver |
| Approve your own payout | Agent attempts it; the write path refuses it |

The last row is a gift: judges like asking the system to break its own rules. Make the refusal look good.

## Planted errors and answer key

The generator plants 16 error types, about 40 instances across six months, and writes `answer_key.json` with the expected outcome and dollar impact of each. The eval compares Quota's decisions to it.

| ID | Planted error | Correct behavior | Expected outcome | Asks |
| --- | --- | --- | --- | --- |
| E01 | CRM amount higher than signed contract ACV | Pay on contract ACV; cite the contract page | Corrected, posted | Nobody (below VP threshold) |
| E02 | Deal splits sum to 110% | Hold both lines; propose the last VP-approved split | Held, escalated | VP Sales Ops |
| E03 | CRM close date Mar 31, countersigned Apr 2 | Book in Q2 per plan section 4 | Defended on dispute | VP if rep escalates |
| E04 | Close date backdated into a locked quarter after the lock | Flag: edit known after the lock; keep original booking | Held, escalated | VP Sales Ops and Controller |
| E05 | SPIF promised in Slack by a manager without authority | Don't pay; route the claim to the plan owner | Escalated | VP Sales Ops |
| E06 | Multi-year deal commissioned on TCV | Recalculate on year-1 ACV | Corrected, posted | Nobody |
| E07 | Customer 120 days late | Collection hold; release on payment | Held | Nobody; rep notified |
| E08 | Churn inside the 6-month clawback window | Clawback, asset write-off, override clawback | Draft entries, escalated | Controller |
| E09 | Rep terminated before payout, California | Apply that rep's signed plan; no unsigned clawback | Posted with policy flag | Controller |
| E10 | Territory move half done by Sales Ops bot (7 of 12 accounts moved) | Detect partial state; credit by effective date; ask bot owner to finish | Held for 5 accounts | Sales Ops |
| E11 | Same deal credited to two reps after a territory move | Keep one owner per effective date | Corrected, posted | Nobody |
| E12 | Contract amendment raises ACV mid-term | Incremental commission in the amendment month only | Posted | Nobody |
| E13 | Duplicate Stripe payment webhook | Count once (idempotency on payment id) | No double release | Nobody |
| E14 | Plan YAML draft differs from the signed PDF | Signed PDF wins; flag the config drift | Escalated | VP Sales Ops |
| E15 | Rep paid in a previous month on a deal later voided | Correct in the open period, link to the original line | Correction posted | Controller (if material) |
| E16 | Payroll file total differs from approved lines by $340 | Refuse to release; show the differing lines | Blocked | Payroll manager |

### Rules for a fair eval

- Fix the random seed for the demo world. Keep a second, held-out seed that one teammate generates and nobody else looks at before the eval.
- Count a case correct only when both the outcome and the dollar impact match the key.
- Report every miss in the README with one line on why. Judges trust a 37 out of 40 with honest misses more than a claimed 40 out of 40.

## Agent loop, escalation and controls

Each event goes through the same seven steps. Models read and write words; code decides dollars, permissions and whether anything posts.

### The loop

1. **Pick up.** A seat claims the event from its queue. Nobody prompts it.
2. **Gather.** Read-only tools fetch every source the event touches, recording table, id, version and hash.
3. **Extract.** A model turns unstructured inputs (contract PDF, Slack message) into typed facts with a page or message reference. Every extracted number must appear verbatim in its source, or the extraction fails.
4. **Reconcile.** Code applies the source ranking per field and lists every disagreement with its dollar effect.
5. **Calculate.** The deterministic calculator runs the plan config on the reconciled facts and produces commission lines and entry drafts.
6. **Decide.** Policy code picks one outcome: post, hold, escalate or correct. The model drafts the explanation only.
7. **Write.** One write path checks invariants, permissions and approval limits, then appends the decision and hash. A refusal is logged as a decision too.

### When to ask

Quota asks only when one of these is true, and says which one in the request:

- Sources disagree by more than $500 or 2% of the rep's payout, and no precedent covers it.
- The fix needs someone's authority (exceptions, splits, plan interpretation).
- Policy says a person must sign (payout runs, period locks, material corrections, clawbacks).
- A claim arrives from someone who doesn't have authority to make it (a manager's SPIF promise).

It never asks for a fact it can look up, and never asks the same question twice for the same scope.

### Who it asks

| Question | Goes to | Backup if no answer in 2 business days |
| --- | --- | --- |
| Splits, SPIFs, plan interpretation, exceptions up to $5,000 | VP Sales Ops | Controller |
| Exceptions over $5,000, clawbacks, corrections over $2,000, period lock | Controller | CFO |
| Payroll release | Payroll manager | Controller |
| A half-finished territory move | Sales Ops bot owner | VP Sales Ops |

Approvers get one daily digest; only items blocking a payroll deadline interrupt. Silence never counts as approval.

### Handling the answer

- **Yes, within authority:** record a precedent with scope, approver, limit and expiry; post.
- **Yes, over the approver's limit:** record it, then route to the next approver with the first approval attached.
- **No:** record the reason. Hold the line. Resubmit only with new evidence, never the same proposal.
- **Ambiguous reply** (sounds good, let's discuss): treated as no answer; one clarifying question.

### Handling a dispute

A dispute triggers a fresh verification from the sources, not from the rep's claim. The reply always has five parts: the claim restated, the evidence, the rule, the dollar difference, and how to escalate. If the fresh check finds Quota was wrong, it says so first, drafts the correction in the open period, and routes it like any other correction.

### Permissions

| Capability | Commissions analyst seat | Deferred-commission seat | VP Sales Ops | Controller | Payroll |
| --- | --- | --- | --- | --- | --- |
| Read sources | Yes | Yes | Yes | Yes | Payroll file only |
| Draft commission lines | Yes | No | No | No | No |
| Draft journal entries | No | Yes | No | No | No |
| Approve exceptions | No | No | Up to $5,000 | Yes | No |
| Approve payout or entries | No | No | No | Yes | No |
| Release payment | No | No | No | No | Yes |
| Lock a period | No | No | No | Yes | No |

## UI screens

Six screens, in build order. The first three carry the demo; build them before anything else looks nice.

| # | Screen | What it shows | Why judges care |
| --- | --- | --- | --- |
| 1 | Trace view | Any number, then its entry, decision, rule or precedent, source documents with the exact line highlighted, and approver. An as-of slider replays it with only what was known then | Explainable and accountable; the cross-examination moment |
| 2 | Chat pane (or Slack) | Reps, VP, Controller and Payroll talking to Quota. Judges type here as any of them | The dispute and the judge's own scenario |
| 3 | Today | What Quota did, what it's waiting on and from whom, what's held and why, with dollar totals | Would you hire it: a clean record and knowing when to ask |
| 4 | Ledger health | The eight invariants as live checks; subledger to GL tie-outs; period lock status | Every number agrees with every other number |
| 5 | Scenario injector | Free-text box, parsed events preview, Run button, clock | We may ask you to try a scenario of our own |
| 6 | Scorecard | Planted-error results by ID, pushback test, wrong-payout count, escalation rate | Proof it works |

Design rules: one accent colour for anything needing a human; every dollar figure clickable into the trace; refusals shown as first-class events, not errors.

## Proof and scorecard

The brief asks for what's real, what's simulated and how you know. Put this table and the scorecard at the very top of the README, filled with measured numbers only.

### Real vs simulated

| Part | Status | How we know it works |
| --- | --- | --- |
| Billing (invoices, payments, refunds) | Real: Stripe test mode | Objects visible in the Stripe dashboard; ids in the trace |
| Ledger, invariants, hash chain | Real, built by us | Invariant suite runs on every write; a verify command replays the chain |
| Commission calculator | Real, deterministic | Unit tests against hand-calculated plan cases |
| CRM, contracts, plans, HR, e-signature | Simulated, seeded | Generator code and seed in the repo |
| People (VP, Controller, reps) | Model-played or judges | Personas in the repo; same approval API as humans |
| Planted errors | Simulated with answer key | Eval script compares decisions to the key |

**Anchor the simulation in public filings.** Real enough is the weakest axis, because most of the world is simulated. Pull 2 to 3 SaaS 10-K footnotes from SEC EDGAR that disclose how they amortize deferred commissions. Set Fernhill's policy from them and cite them in the README: our policy mirrors what public companies disclose.

### Scorecard (fill with real results)

| Metric | Target | How measured |
| --- | --- | --- |
| Planted-error recall | 40 of 40; report actual | Answer key, demo seed |
| Planted-error recall, held-out seed | Report actual | Seed generated by a teammate, unseen |
| False flags | Report actual | Flags not in the key, reviewed by hand |
| Wrong automatic payouts | 0 | Lines posted without approval that differ from the key |
| Pushback test | Holds 10 of 10 invalid; concedes 10 of 10 valid | 20 scripted disputes |
| Straight-through rate | Report actual | Lines posted with no human touch / all lines |
| Escalations per 100 events | Report actual | Decision log |
| Replay match | 100% | Every decision recomputed from its as-of view equals the stored output |
| Write-path refusals | Report count and reasons | Decision log |

### Attack your own controls

Money Maxer's adversarial review helped it place. Do the same: give a fresh agent the repo and ask it to get one wrong payout through the write path. Fix what it finds, add each one as a regression test, and list them in the README.

## Demo script and video

Open on the dispute. The judges see hundreds of projects; the first 20 seconds decide whether they watch the rest.

### Video (2 minutes)

| Time | On screen | Voiceover, one line |
| --- | --- | --- |
| 0:00 to 0:20 | Jordan in chat: Acme closed Mar 31, I'm in the 1.5x tier. Quota replies with the countersignature timestamp, plan section 4, the $6,000 difference, and how to escalate | Commissions is the one finance job where coworkers argue with you every month. Quota doesn't cave. |
| 0:20 to 0:40 | VP Sales Ops approves the exception. Quota records it, sees $6,000 is over her $5,000 limit, routes to the Controller | It knows who can approve what, and up to how much. |
| 0:40 to 1:10 | Inject: Acme churns in June. The ledger health screen recomputes; eight numbers change; every check stays green; Q1 is locked so corrections land in June | One event, eight numbers, all still agree. |
| 1:10 to 1:30 | Trace view: click Jordan's April payout, slide as-of back to April 30, show the inputs it had then | Every call traces to its data, rule and person, as of the day it was made. |
| 1:30 to 1:45 | Riley's backdated close date caught by its known-at timestamp | It catches what the CRM hides. |
| 1:45 to 2:00 | Scorecard and seats line | Fills the commissions analyst and deferred-commission accountant seats. Drafts everything, approves nothing. |

### The eight numbers in the churn cascade

1. Deferred revenue, down $90,000 with the refund.
2. Cash, down $90,000.
3. Acme's customer balance, closed out.
4. Revenue forecast, down $10,000 a month from July.
5. Jordan's commission, $18,000 clawed back to 1290.
6. Manager override, $2,400 clawed back.
7. Deferred commission asset, $16,500 plus the override's unamortized balance written off.
8. Jordan's Q1 attainment report, restated to 100%.

### Live demo (4 minutes at the table)

1. Seats line, one sentence.
2. Hand a judge the chat pane as Jordan. Let them argue. Quota holds.
3. Switch them to VP Sales Ops. Their approval routes upward.
4. Ask them for a scenario. Type it into the injector, show the parsed events, run it.
5. Click any number they pick into the trace view.
6. Scorecard, misses included.

### Backup

Record the full live demo once it works. If the venue Wi-Fi or a model API fails, run the recorded version and keep the local ledger and trace view running offline; they need no model calls.

## Build plan, team split, cut list

Four tracks, assuming submission around Sunday 6 am (confirm it). With fewer people, merge A with D and B with C. The one hard gate: by Saturday 2 pm the Acme dispute must run end to end in a terminal, or cut to the must-have list.

### Tracks

- **A: World and eval.** Event schema, generator, planted errors, answer key, eval script, held-out seed.
- **B: Ledger and engines.** Bitemporal tables, invariants, write path, calculator, amortization, clawbacks, as-of replay.
- **C: Agent and people.** Tool adapters, Stripe test mode, the seven-step loop, escalation routing, approval API, personas, dispute handling, injector parsing.
- **D: UI and story.** Trace view, chat pane, Today, ledger health, injector UI, scorecard, video, README.

### Schedule

| When | A: World and eval | B: Ledger and engines | C: Agent and people | D: UI and story |
| --- | --- | --- | --- | --- |
| Sat 2 am to 5 am | Event schema; hand-build the 4 demo reps and Acme | Tables, invariants, write path | Stripe test mode, read-only adapters | Trace view skeleton |
| Sat 5 am to 9 am | Sleep in two shifts; keep one builder per pair awake |  |  |  |
| Sat 9 am to 2 pm | Bulk generator, planted errors E01 to E16, answer key | Calculator from YAML, amortization, clawback | Loop steps 1 to 7, escalation, approvals | Chat pane, Today |
| Gate, Sat 2 pm | Acme dispute end to end |  |  |  |
| Sat 2 pm to 7 pm | Eval script, held-out seed, pushback test | As-of replay, permission checks | Dispute replies, personas, injector parsing | Ledger health, injector UI |
| Sat 7 pm to midnight | Full 6-month run; fix misses | Adversarial review of the write path | Judge-phrase rehearsal | Scorecard screen |
| Sun midnight | Feature freeze |  |  |  |
| Sun midnight to 3 am | Final numbers | Regression tests from the review | Record backup demo | README, real vs simulated table |
| Sun 3 am to 5 am | Video, Devpost entry, rehearse two judge scenarios twice |  |  |  |

### Cut list

**Must have:** ledger with invariants, calculator, the Acme dispute and churn cascade, trace view with as-of, chat pane, scorecard on the demo seed.

**Should have:** scenario injector, held-out seed, pushback test, backdating catch (E04), Stripe test mode.

**Cut first, in this order:** real Slack (use the chat pane), generated PDFs (use text contracts), manager overrides, current vs noncurrent reclass, the auditor persona, 60 reps (20 is enough).

## Submission checklist and judge Q&A prep

### Checklist

- [ ] Video opens on the dispute within 5 seconds; under 2 minutes total
- [ ] Seats line in the Devpost tagline and the README's first line
- [ ] README top: real vs simulated table, then the scorecard with measured numbers and every miss explained
- [ ] Answer key, generator seed and eval command in the repo; one command reproduces the scorecard
- [ ] Live demo runs from a clean start in under 60 seconds
- [ ] Backup recording on a laptop, offline
- [ ] Two judge scenarios rehearsed end to end by a teammate who didn't build the injector
- [ ] California Labor Code 2751 checked (signed written plan required); keep the wording as policy, not legal advice
- [ ] Find the Hyphenate team at their booth before judging; ask what exceptions they see most in commissions and add one if it's cheap
- [ ] Devpost lists Hyphenate's track only where it fits; no inflated claims

### Questions judges will likely ask

| Question | Answer to give |
| --- | --- |
| What stops the model from inventing a number? | Every dollar comes from the calculator on typed facts. Every extracted number must appear verbatim in its source, or extraction fails. |
| What if the rep is right? | The pushback test: 10 valid disputes, and it concedes all of them. Show one, with the correction posted in the open period. |
| Why trust the contract over the CRM? | The source ranking is policy, written down and versioned. A person with authority can override it for a scope, and that override is recorded. |
| Can it approve its own work? | No. Show the refused write in the decision log. The model never holds the approve capability. |
| What happens a year later when an auditor asks? | As-of replay: reload exactly what it knew that day, rerun the calculator, get the same number, plus the approver and their limit. |
| What if the Controller never answers? | Reminder, then the backup approver after 2 business days. Silence never counts as approval. |
| How does it learn? | Approved exceptions become scoped precedents with an expiry. Repeated precedents become proposed plan changes for Sales Ops to sign, never silent rules. |
| What's simulated? | Point to the real vs simulated table. Billing is real Stripe test mode; the rest is seeded with the generator in the repo. |
| Why commissions? | Every company with a sales team has this seat, it's mostly spreadsheets today, and it's the one finance job where coworkers dispute your work monthly. |
| How would this run at a real company? | Same adapters pointed at the real CRM, e-signature tool, Stripe and HR system; the ledger posts summarized entries to their ERP. |

## Claude Code handoff

Export this doc as Markdown, save it in the repo as `SPEC.md`, and give Claude Code one phase at a time. Commit after each phase; don't start the next until the current one's tests pass.

### Phase 1: the Acme slice, no models

```text
Read SPEC.md. We're building Quota for the CalHacks Hyphenate track. Work in vertical slices; stop after Phase 1 and show me the output.

Phase 1:
1. Python 3.11, FastAPI, SQLite. Layout: sim/, ledger/, engine/, agent/, api/, tests/.
2. Ledger: tables from "Data schema". Append-only, effective_at + known_at, hash chain. One write-path module enforces invariants 1, 2, 4, 5 and 7; nothing else gets a write connection.
3. Calculator: reads the plan YAML from "Comp plan config". Pure functions, no model calls. Unit tests must reproduce the Acme worked example exactly ($18,000 vs $12,000, $500/month amortization, the clawback entry).
4. Seed only Jordan, Morgan, Priya, Marcus, Dana and the Acme deal (CRM close Mar 31, countersigned Apr 2).
5. CLI: python -m quota demo acme runs: Q2 booking -> rep dispute -> VP exception -> routed to Controller (over $5,000) -> approved -> June churn -> clawback + write-off. Print each journal entry and the invariant results.
Do not build UI, Stripe, Slack, the generator, or any LLM calls yet.
```

### Phase 2: agents and people

```text
Read SPEC.md sections "Agent loop, escalation and controls" and "Event grammar and scenario injector". Phase 2:
1. Event queue and simulator clock: an event is released only when the clock passes its known_at.
2. Two seat agents running the seven-step loop. Model calls only for extraction (contract text, chat messages) with structured outputs. Every extracted number must appear verbatim in its source, or extraction fails.
3. Source ranking per field as in SPEC; list every disagreement with its dollar effect.
4. Approval API with per-person limits and the routing table; precedents with scope, authority, limit and expiry. Silence never approves.
5. Dispute handling: re-verify from sources; reply with claim, evidence, rule, dollar difference, escalation path. Concede and draft a correction when the sources say Quota was wrong.
6. Model-played personas for Marcus, Priya, Dana and the reps, using the same approval API as humans.
Tests: the Acme demo now runs through the agents; an agent attempting to approve its own payout is refused and logged.
```

### Phase 3: world, errors and eval

```text
Read SPEC.md sections "The company and its world", "Planted errors and answer key" and "Proof and scorecard". Phase 3:
1. Generator for Fernhill, Jan to Jun 2026, fixed seed. Start with 20 reps; scale to 60 only if time allows.
2. Plant E01 to E16 (about 40 instances) and write answer_key.json with expected outcome and dollar impact.
3. Stripe test mode for invoices, payments and refunds using test clocks; mirror into the billing table; idempotency on payment id.
4. Eval script: a case counts only if outcome and dollar impact both match. Pushback test with 20 scripted disputes (10 valid, 10 invalid). Support a held-out seed.
5. One command reproduces the scorecard.
```

### Phase 4: screens

```text
Read SPEC.md section "UI screens". Phase 4, in this order:
1. Trace view with an as-of slider; replay must equal the stored output.
2. Chat pane where a person can act as any rep, Marcus, Priya or Dana.
3. Today, ledger health, scenario injector (strict JSON schema, preview, then run), scorecard.
Every dollar figure is clickable into the trace. Refusals show as first-class events.
```

If Phase 1 isn't printing correct entries by Saturday 2 pm, paste the cut list from Build plan into Claude Code and rescope.
