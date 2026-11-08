# Quota — Devpost form text

Paste straight into Devpost's fields. Every number here is measured by a command in the repo.

---

## Tagline / elevator pitch (under 200 chars)

An autonomous commissions desk. It fills the commissions analyst and deferred-commission accountant seats: drafts every payout and journal entry, approves none, and defends every number it writes.

---

## The seats it would fill (Hyphenate asks for one line)

Quota fills the **commissions analyst** and **deferred-commission accountant** seats at a SaaS company: it drafts every commission line, every payout file and every ASC 340-40 journal entry, and it approves none of them.

---

## Inspiration

My cofounder's father has run a loan brokerage for thirty years. The lending isn't the hard part — paying for it is. He has a large team of telecallers working leads, and **three people whose entire job is tracking who sold what and what each telecaller is owed**. The first week of every month goes to one argument: who gets paid what. The sales sheet says one thing, the payout sheet says another, and a telecaller says he was promised more over WhatsApp.

That last one is the interesting part. Every reconciliation system in finance compares structured records. The hardest commission problems aren't in structured records at all — they're in a message somebody sent.

Three people, one object, and the same argument every month — that is exactly the shape Hyphenate asked for: a few seats that work closely together. So we picked the one finance object nobody has made autonomous. A commission line is four things at once: a payroll liability, a capitalized asset amortized under ASC 340-40, a legal obligation under state wage law (in California, commissions are advances until earned), and an argument with a salesperson who has your phone number. Get it wrong and you have simultaneously misstated expense, underpaid a person, broken a wage statute, and started a fight.

---

## What it does

Quota owns one object — the commission line — from signed contract to paid paycheck to booked expense.

**It catches what nobody would have caught.** A rep emailed a customer: *"Between us, if your team hasn't adopted it by end of Q3 we'll let you out of year two, no penalty."* The signed contract has no exit clause. That sentence shares no words with "termination" or "cancellation", so keyword search misses it (BM25 finds 5 of 12 planted promises in 2,940 messages; semantic finds 10 of 12). Elastic finds it by meaning, a model reads it, and then **code** checks the quote appears verbatim in the body and that the contract doesn't already grant it. Result: decision D-0014, $6,000 held out of May payroll, reason OFF_CONTRACT_COMMITMENT, approval A-0009 opened for the Controller — who is also told revenue may be misstated, because an undisclosed exit right changes the term.

**It defends a decision when questioned.** A rep claims his $120,000 Acme deal closed Mar 31; the e-signature log says countersigned Apr 2, 10:14 AM Pacific. Quota holds, and shows the CRM date struck through against the countersignature: $12,000, not the $18,000 claimed. He pushes back with no new evidence — held again. He escalates. His VP approves, but it's over the VP's $5,000 limit, so it routes on to the Controller. Then someone asks the agent to approve it itself: refused, and logged as R-0001 with NO_CAPABILITY, INV5_SELF_APPROVAL, INV5_WRONG_APPROVER.

**It recovers when things go wrong.** Acme churns in June and gets $90,000 back. One event moves eight numbers: deferred revenue, cash, customer balance, forecast, the rep's commission, the deferred-commission asset, expense and attainment. $18,000 clawed back, $16,500 of unamortized asset written off, $1,500 already amortized. Because the rep is in California and his signed plan makes commissions advances until earned, the clawback becomes a receivable recovered from his *next* commissions — never by billing him. Every invariant stays green and the books still balance.

---

## How we built it

**One writer.** Every write — a fact, decision, commission line, journal entry, approval, payroll release — goes through a single `WritePath` that checks the principal's capability, then seven invariants, then writes in one transaction or logs a numbered refusal:

- INV1 every entry balances
- INV2 nothing posts into a locked period
- INV3 every subledger ties to its GL account
- INV4 no entry or line without a decision; no decision with empty inputs
- INV5 no self-approval, only the routed approver may decide
- INV6 a payroll file's total equals its approved lines
- INV7 splits sum to exactly 100%

**Models read words; code decides dollars.** Each seat runs a seven-step loop: pick up an event from its own queue, gather read-only (recording table, id, version, sha256), extract with a model whose quotes must match the source verbatim, reconcile through a written source ranking, calculate deterministically, decide, write. A model may name a field. Only code may produce a dollar, a date or an outcome.

**An append-only, bitemporal, hash-chained SQLite ledger**, 24 tables, no row ever updated or deleted. Every row carries `known_at` as well as its effective date, so any screen or decision replays *as of* a past moment with nothing learned later. Each insert appends `sha256(previous, row)`; `quota verify` recomputes the chain from genesis. 580 of 580 decisions replay identically from their recorded inputs.

**Confidence as a weakest link.** Every line carries a score: the *minimum* of six evidence factors × 100 — booking date, amount, owner splits, extraction, rule coverage, integrity — each with a sentence saying why. Never a model's self-rating. An average would let five good signals hide one broken one; the integrity factor alone is capped at 0.40 by a backdated edit.

**Elastic as the evidence layer.** Three indexes (`quota-comms`, a one-way hash-verified mirror in `quota-decisions`, `quota-findings`), a daily `promise-sweep` Workflow, and an Agent Builder agent with seven read-only tools, also served over MCP. Nothing in that layer holds a write capability, so no hallucination can move a cent.

**Onboarding through screens, not integrations.** Handed a login to a real HR web app (a BambooHR clone on Bun/Prisma), a computer-use agent finds the directory, opens a profile, reads the Job tab — and writes down what it learned as a playbook. Re-syncing runs the playbook over every employee with **zero model calls**, verified 100% against the app's own API, with the app's audit log showing **zero writes**.

**Stack:** Python 3.11, FastAPI, SQLite, React + Vite, Elastic Serverless 9.6 (Jina embeddings + reranker, Agent Builder, Workflows, ES|QL, MCP), Stripe sandbox with test clocks, Claude Opus 5.5, Bun + Prisma for the HR clone.

---

## Proof it works — what's real, what's simulated, and how we know

**Real, built here:** the ledger, invariants and hash chain; the commission calculator, amortization and journal entries; the decision log and replay; the API and screens.
**Real, external:** billing in a Stripe sandbox with test clocks; the HR app, read through its screens.
**Simulated, seeded:** CRM, contracts, e-signature log, plans; the people (scripted in the demo, rule-played in the eval, played by Claude with `--live`).
**Simulated with an answer key:** 16 kinds of planted error, 40 cases per seed.

### Scorecard — `python -m quota eval --seed 7` and `--seed 1729`

20 reps, Jan–Jun 2026, ~245 deals, ~1,500 events. A case counts only when **both** the outcome and the dollar amount match the key.

| Metric | Seed 7 (development) | Seed 1729 (held out) |
|---|---|---|
| Planted-error recall | 40 / 40 | 40 / 40 |
| False flags | 0 | 0 |
| Wrong automatic payouts | 0 | 0 |
| Pushback test (20 scripted disputes) | held 10/10 invalid, conceded 10/10 valid | same |
| Straight-through rate | 94.7% | 94.9% |
| Escalations per 100 events | 0.7 | 0.7 |
| Replay match | 100% (554 decisions) | 100% (580 decisions) |
| Write-path refusals | 2, both INV6_PAYROLL_MISMATCH | 2, same |

**The honest miss.** On the first run of held-out seed 1729 one case came back `held` where the key said `posted`. The error was in the **key**: the plant made a departing rep's own deals collect before his termination date but forgot a 30% split he held on another rep's deal, which never collected. Quota held that line, which is what the signed plan says. We fixed the plant and reran.

**Billing, run for real.** The same seed-7 run with `--stripe` reproduced the column exactly while billing ran in a Stripe sandbox: 81 test clocks, 241 customers and invoices, 210 card payments, 3 refunds. Stripe's own search confirms 241 invoices (210 paid, 31 open at June 30) and 3 refunds totalling **$214,916.66**. All 454 billing rows carry Stripe ids.

**Backtest against last quarter.** Before anyone asked it anything, Quota re-ran Q1 from the spreadsheet the company actually paid from: **137 of 140 payouts reproduced**; the three that didn't were spreadsheet mistakes worth **$14,948.96 overpaid**.

**Elastic, measured 2026-10-25** (Serverless 9.6, `.jina-embeddings-v5-text-small`, `.jina-reranker-v3.5`): 2,940 of 2,940 messages embedded and indexed in ~40s; `MIRROR OK seq=368`. Retrieval over 12 planted promises and 12 decoys, union of six intents, top 20 each — keyword 5/12 (3 decoys), semantic **10/12** (11 decoys), hybrid+rerank 8/12 (11 decoys). The gate end to end: **12/12 promises held, 0/12 decoys held, 52/52 cases correct on both seeds.** Precedent recall for approvers: **hit@3 6/6**.

**Policy anchored in public filings.** The capitalization policy follows Snowflake's FY2025, Amplitude's FY2024 and DocuSign's FY2024 10-K notes, read from SEC EDGAR.

**What the scorecard does not show.** The 16 error types and Quota's handling of them were built in the same phase, so the held-out seed tests a new *arrangement* of the same kinds of error, not unseen kinds. Expected amounts are priced by the same calculator on ground-truth facts, so the amount check tests policy, not arithmetic (the arithmetic is covered by hand-calculated unit tests). Seed 1729 was chosen by the builder, not hidden by a teammate.

---

## Challenges we ran into

**Semantic search is recall, not precision.** It found 10 of 12 planted promises — and pulled in 11 of the 12 decoys. That is not a bug we hid; it drove the architecture. Retrieval proposes, a model reads one message at a time, and then two deterministic checks decide: the quoted words must appear verbatim in the body, and the signed contract must not already grant what was promised. Each layer can be wrong on its own without money moving wrongly. Hybrid + rerank actually scored *worse* than semantic alone (8/12) because the reranker dropped two true positives — we report that because it's what happened.

**Our own auditor got an answer wrong, and the mirror caught it.** In the live rehearsal the Elastic agent said a VP had approved an exception "for himself". He hadn't — our decision mirror had taken his grant message as the rep. We found it because `quota elastic verify` rebuilds every mirrored document from the ledger and compares it, so drift is loud. Fixed, and the wrong answer is still printed in `docs/ELASTIC_LIVE.md`, because a system that only publishes its wins is not an audit trail.

**We pressure-tested the build against its own spec and found 14 real defects**, all now fixed with regression tests: deals with no signed contract were commissioned on CRM data alone; rejected exceptions could be resubmitted unchanged; a split edited to 110% after booking went unnoticed; held credit still counted toward attainment; an approval could be decided twice; a clawback receivable sat in 1290 forever; one bad injected event crashed the seat and every later request. After all fourteen the eval was unchanged: both seeds 40/40, 0 false flags, 0 wrong payouts.

---

## Accomplishments we're proud of

- **Zero wrong automatic payouts and zero false flags on a held-out seed**, with the outcome *and* the dollar amount both required to match.
- **580 of 580 decisions replay identically** from the rows they recorded reading.
- A commissions agent that tells a **Controller her revenue might be wrong, because of an email**.
- **Four refusals in a row** in the dispute path, including the system refusing itself.
- An agent that **onboards through a real app's screens** and then re-syncs with zero model calls, verified against that app's API, with zero writes in its audit log.

---

## What we learned

Autonomy in finance isn't about how much the agent does — it's about how precisely it's prevented from doing the rest. Every useful property we have came from a constraint: one writer, no approval capability, verbatim-only extraction, append-only rows, replay from recorded inputs, confidence as a minimum rather than an average. The interesting engineering was deciding what the system must *not* be able to do, and then making that unarguable in code rather than in a prompt.

---

## What's next

- Manager overrides, and the 1350/1450 current-versus-noncurrent split of deferred commissions (both deliberately cut for scope).
- Multi-entity and multi-currency. A non-USD contract is `NO_RULE` today: no line is written and the plan owner is asked. That's the right failure, but it's a failure.
- Real CRM and e-signature connectors, replacing the simulated ones.
- An adversarial agent attacking our controls. We wrote the attacks as tests, which is weaker — our tests can only try what we thought of.

---

## Built with

python, fastapi, sqlite, react, vite, typescript, elasticsearch, elastic-agent-builder, elastic-workflows, esql, jina-embeddings, mcp, stripe, stripe-test-clocks, claude, anthropic, computer-use, bun, prisma, asc-340-40, sec-edgar, pytest, playwright

---

## Try it

```bash
pip install -e .
python -m quota demo acme          # the whole Acme story, end to end
python -m quota eval --seed 7      # the scorecard, against the written answer key
python -m quota eval --seed 7 --stripe
python -m quota serve              # the screens; --live plays the people with Claude
python -m quota verify             # re-hash the chain from genesis
python -m quota replay D-0005      # recompute one decision from its recorded inputs
pytest -q                          # 284 tests
```

At judging we hand over the keyboard. `Ctrl-K` opens the scenario list; the **Inject** tab takes a plain sentence ("Acme churns in June with money back"), previews the events it would create, lets you edit a field, and runs it.
