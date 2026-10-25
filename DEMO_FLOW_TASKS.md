# Quota — CFO demo flow: build tasks (D1–D7)

Goal: the demo reads as a CFO using Quota on a normal morning. They see every input, every rep's pipeline and pending commission, open a rep's contracts, follow links to the other reps on a deal, see how Quota settled each commission and how sure it is, and answer the few questions it routes to them.

Rules:
- Reuse what exists: Case view, Trace, Ledger health, Intake, scenarios, checkpoints.
- Don't rewrite the engine. Run `pytest -q` after each task, then commit `D<n>: …`. Log choices in ASSUMPTIONS.md.
- The UI computes nothing: every figure comes from the API, and every dollar opens the trace.
- Anything simulated (pipeline stages, CSAT) is labeled "simulated".

## D1 — Confidence score (backend, `quota/engine/confidence.py`)

The score is **derived from evidence, never a model's self-rating**. It is the weakest-link minimum of six factors, × 100, and every factor names its reason.

| Factor | 1.00 | 0.80 | Lower |
|---|---|---|---|
| Booking date sources | all agree | disagree, settled by the written ranking (R-BOOKING-01) | 0.30 unresolved above materiality |
| Amount sources | all agree | disagree, settled by ranking (R-AMOUNT-01) | 0.30 unresolved |
| Owner / splits | one owner or a VP-approved split summing to 100% | CRM split only | 0.00 splits ≠ 100% or blank owner |
| Extraction | numbers verbatim in source **and** a second source agrees | verbatim only (0.90) | 0.50 model-only |
| Rule coverage | plan and clauses all known | a saved precedent applies (0.90) | 0.00 `NO_RULE` |
| Integrity | no flags | — | cap 0.40: backdated edit, beneficiary edit, off-contract promise, approver conflict |

Collection status is not a confidence factor. An unpaid invoice is a **hold** (pay on collection), shown separately.

**Bands:**
- **≥ 90:** cleared to the payout file. The Controller still signs the batch.
- **70–89:** cleared with a note to the rep and the analyst queue.
- **< 70:** held, and the right person is asked (D5).

**Storage and API:**
- Store `confidence` and its `factors` in each commission decision's `output_json` so replay reproduces them.
- API: `GET /api/lines/{id}/confidence`.

**Eval:** add to the scorecard, both measured on Fernhill seed 7 and reported as "separation", not calibration.
- share of planted-error lines scored < 70 (target: all)
- share of clean lines ≥ 90

**Tests:**
- Acme: CRM vs. contract settled by rules → 80, band "cleared with note"
- Summit side letter → ≤ 40, held
- 110% split → 0, held
- clean deal → 100

## D2 — Sources rail and raw viewer (Home)

New **Home** tab, first in the nav.

**KPI strip** for the current period:
- pending commission $
- cleared %
- held $ (with counts by reason)
- "needs you" count

**Sources rail:** one card per input, showing count, last sync and how it arrived:
- Contracts (PDF/CSV)
- Email & Slack (Elastic index count)
- CRM (Salesforce export)
- HR (BambooHR clone: API or computer-use agent [if C2/C3 built])
- Stripe (sandbox)
- Q1 payout spreadsheet (backtest "137 of 140")

**Raw viewer:** clicking a card opens a drawer with the actual rows. Extracted fields are highlighted; a contract PDF page shows its quoted spans.

API: `GET /api/sources`, `GET /api/sources/{kind}/rows?q=`.

## D3 — Reps board

A table of reps, sorted by "needs attention".

**Columns:**
- rep
- open pipeline $ (by stage)
- booked this quarter
- attainment %
- pending commission $
- lowest confidence
- status (Cleared / Note / Held)

**Open pipeline:**
- The generator has mostly closed-won deals. Add 3–6 open opportunities per rep (stages: Discovery, Proposal, Negotiation, Contract sent), labeled simulated.
- Open deals show **forecast commission** at the plan rate, clearly marked "not earned".

API: `GET /api/reps`.

## D4 — Rep detail → contract cards → connected reps

`/#rep/{id}`:

1. **Pipeline lane:** open deals by stage, each with forecast commission.
2. **Contracts:** one card per booked contract:
   - customer, ACV, term, billing, ramp
   - clauses (e.g. 90-day termination for convenience), countersigned date (Pacific)
   - paid / overdue
   - commission line, confidence badge
3. **Connected people** chips on each card: split partners with their shares, manager override (Morgan 2%), SE team pool, outside partner (Northstar Partners LLC, paid via Stripe Connect [if C6 built]). Each chip opens that person's view of the same contract.
4. **Expansions:** a contract amendment shows as a child card under its parent, with **incremental** commission only (E12). If the expansion was sold by an account manager, their chip and credit appear there.
5. **Split bar:** a contract with several recipients shows a single 100% bar (e.g. Orbit $200k: Alex 50%, Riley 20%, Northstar 20%, SE pool 10%). Each segment links to its payroll line or Stripe transfer.

API: `GET /api/reps/{id}`, `GET /api/contracts/{id}` (includes `recipients`, `children`, `parent`).

## D5 — Commission calibration panel and "Needs you" inbox

**Calibration panel** (opens from any contract card):
- **Evidence list:** each source as a row (CRM, contract, e-signature, billing, HR, messages), with the value it says, a ✓ "agrees" or ! "disagrees" label (text, not color alone), and `file · row`.
- **The rule that settled each disagreement.**
- **Computed amount**, with base and accelerator portions.
- **Confidence** and its six factors (D1).
- **Outcome:**
  - cleared → "In May payout file"
  - held → "Asked <person>: <question>" with an Approve / Reject / Reply box for that person

**Needs you inbox** (Home, right side): every held line and pending approval, grouped by person.

| Person | Gets asked about |
|---|---|
| VP Sales Ops | splits, exceptions up to $5k, plan readings |
| Controller | integrity flags, off-contract promises, legal (termination, CA recovery), corrections, payout batch |
| Payroll | release |
| Sales Ops | CRM data fixes (blank owner) |

Each item shows the pre-written question, the dollar impact and the evidence.

When a person answers, the answer is saved as policy and the item clears. The approval chain (Marcus over limit → Priya) and refusals (R-0001) appear as small timeline entries inside the item.

## D6 — Presenter path for the CFO flow

Add a second presenter script `cfo` (keep the existing one):
1. Home → sources rail → open the Acme contract PDF and one email in the raw viewer
2. Reps board → Jordan
3. Acme contract card → calibration panel (80, cleared with note) → Jordan's dispute thread inside the panel
4. Orbit split → Alex chip → Riley's view → the expansion card
5. Needs you → Summit side letter (≤ 40): Priya's decision; Acme exception chain as one item
6. Advance to June → Ledger health cascade → Ctrl-K Birch churns
7. Trace with as-of → Scorecard
8. Close-day card [if C10 built] or Home with "Needs you: 0"

## D7 — Polish and checks

- Same visual system as R7: Inter, tabular numbers, Pacific times, one accent for "needs a person".
- Playwright test of the `cfo` presenter path end to end, offline.
- README: one screenshot each of Home, rep detail and the calibration panel.
