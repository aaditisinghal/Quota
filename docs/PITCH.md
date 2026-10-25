# Quota: who it's for, and the pitch

Working notes for selling Quota beyond the hackathon. The build itself runs on a fictional company (Fernhill); use
this framing only when someone asks who would buy it. Figures marked *assumed* are placeholders, not measurements.

## What it is

Quota is a self-running commissions desk. Every month it:

- reads signed contracts, the CRM, billing and each rep's signed comp plan;
- works out what each salesperson is owed, and holds or flags anything that doesn't add up;
- books the accounting entries (ASC 340-40: commission cost spread over the contract it won);
- routes every approval to the person allowed to give it;
- answers reps' disputes with evidence.

It drafts everything and approves nothing. Every number traces back to its source documents, the rule that applied
and the person who signed off.

**Seats it fills:** the commissions analyst, and the accountant who books commission costs under ASC 340-40.

**The problem:** commissions sit between Sales and Finance, and at most companies they're still spreadsheets. The
result is overpayments, slow payouts, reps who don't trust their statements, and an audit trail nobody can
reproduce.

**Who buys it:** CFOs and Controllers at B2B companies with roughly 50 to 1,000 reps and complicated plans,
especially companies preparing for an IPO, where auditors start testing these controls.

## Where it sits: one object, three functions

Quota owns one object, the commission line, from signed contract to paid paycheck to booked expense. It reads from
Sales Ops and Revenue Accounting, hands finished drafts to Payroll and the general ledger, and never edits another
team's system. (The full table and the eight-case escalation contract are in `SPEC.md`, "Scope".)

**Pitch line:** Quota owns every commission from contract to paycheck to ledger. It reads Sales and Revenue data,
hands Payroll and the GL finished drafts, and asks a human only when policy says it must.

The boundary is also the answer to "isn't this CaptivateIQ?" Those tools calculate payouts for a comp admin. Quota
does the work between the calculation and the books, and stops at clearly defined handoffs.

## The pitch to Anthropic's CFO (Krishna Rao)

"Your sales team is growing faster than your finance team can. Every new rep adds commission calculations,
disputes, and a deferred-cost schedule your auditors will test, and an IPO puts all of it under an audit
microscope. Quota does the commissions job itself: it pays reps from their signed plans, catches what the CRM gets
wrong, books the accounting, and routes every exception to the right approver. It can't approve its own work, and
every number traces to the contract, rule and person behind it. You stop hiring a comp analyst for every hundred
reps, you stop overpaying, and the audit file builds itself."

Lead with control and IPO readiness, not headcount. Fortune reported in March 2026 that an Anthropic IPO could
come as early as 2026, so audit-ready controls are what that CFO cares about.

## Does it save money? Illustrative math (all inputs assumed)

Anthropic's rep count and pay mix are unknown; swap in real figures.

| Input (assumed) | Value |
| --- | --- |
| Quota-carrying reps | 300 |
| Average annual commission per rep | $150,000 |
| Annual commission spend | $45.0M |
| Commission team (1 analyst per 100 reps, plus half an accountant) | 3.5 people at $200k fully loaded = $700k |

| Savings | Calculation | Per year |
| --- | --- | --- |
| Overpayments caught | 1% of $45M | $450,000 |
| Staff time | About 65% of 7,000 hours automated = about 4,500 hours at $100/hour | $450,000 |
| **Total** | | **About $900,000** |

Why 1% on overpayments: Xactly's 2018 survey of 200+ companies found most pay commissions inaccurately, with an
average error rate above 5%. That's vendor research and it's old, so 1% is the conservative figure to defend. Both
savings lines grow with every rep added.

Not counted: faster month-end close, lower audit cost, and the IPO risk of a control failure in a cost that shows up
on the balance sheet.

## What Anthropic's CFO would push back on

- **Usage-based deals.** Anthropic sells committed spend plus overages: paying commission on commitments versus actual usage. Not
  modelled; the calculator pays on signed ACV.
- **Cloud marketplace channels.** Deals sold through AWS Bedrock and Google Vertex need partner-credit rules. Not modelled.
- **Fictional data.** The prototype runs on a made-up company. Pitch it as a design partnership: "Give us one
  quarter of history and we'll reproduce last quarter's payouts and show every difference." That is also the best
  answer to "how would this work at a real company?"

## Sources cited in the original notes

- Fortune: Anthropic CFO and IPO risk (March 2026)
- Anthropic: Krishna Rao joins as CFO
- Xactly 2018 sales compensation study
- HR Dive: 80% of companies pay inaccurate commissions (2017)

These were gathered outside this repo and are not re-verified here.
