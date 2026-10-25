# Quota — Demo Script (3:45)

**The ride:** you pick the judge up at Point A, the first week of the month at a company that pays people on deals, when nobody trusts the numbers. You drop them at Point B: the books are closed, every payout traced, and nobody argued. Each stop is one screen. Never show a screen without saying what pain it removes.

**Rules for the driver**
- Talk about people, not features: Jordan, Marcus, Priya, your dad.
- Every number on screen comes from the running system. Point at it; don't read it.
- One sentence of "why" before every click.
- The judge drives at Stop 5. Hand them the keyboard.

---

## Stop 0 — Point A: the problem (0:00–0:30)

**On screen:** a single photo or a black slide. No product yet.

> "My dad has run a home-loan brokerage in Gurgaon for 30 years. Banks pay him a commission on every loan, and he pays out to [his relationship managers, telecallers and outside agents]. The first week of every month goes to one argument: who gets paid what. The bank's statement says one thing, his records say another, and an agent says he was promised more on WhatsApp. [Fill in one real number: days lost a month, or one payout that went wrong.]"
>
> "It's not just him. Any company that pays people on deals has this week. A U.S. software company with 60 salespeople runs the same fight across Salesforce, DocuSign, Stripe, payroll and a spreadsheet."

*Replace the brackets with what's true for your dad's business. Don't guess. One real detail beats three made-up ones.*

## Stop 1 — The new hire shows up (0:30–1:10): onboarding

**On screen:** Quota's People tab, empty. A browser window with BambooHR.

> "Every autonomous finance tool dies in onboarding: weeks of integrations before it does anything. Quota onboards the way a new hire does. It's handed an HR login and reads the screens."

1. Play 10 seconds of the recorded **exploration** run: the agent finds the directory, opens a profile, reads the Job tab.
2. > "It only had to work out the screens once. It wrote down what it learned as a playbook."
3. Click **Re-sync**: the playbook runs live over every employee. Point at the counter.
4. The header lands: **"N employees · 0 model calls · verified 100% against the HR API · 0 writes."**
   > "Two proofs: every number it read matches BambooHR's own API, and BambooHR's own audit log shows it changed nothing."
5. The People table fills: base pay from HR, commission plans learned from the signed PDFs, attainment.
   > "Its first day, before anyone asked it a thing, it also reran last quarter's payouts: 137 of 140 match, and the 3 that don't are spreadsheet mistakes worth $14,948.96."

## Stop 2 — A rep pushes back (1:10–2:00): the case

**On screen:** the Case tab. Presenter mode on.

> "End of April. Jordan sold Acme a $120,000 contract and thinks it counts toward last quarter, where he'd earn a 1.5x rate."

1. **→** Jordan: "Acme closed Mar 31." Quota holds. Point at the evidence column: the CRM says Mar 31, struck through; the signed contract says **Apr 2, 10:14 AM Pacific**.
   > "It doesn't argue from opinion. The signed contract decides, and it shows the difference: $12,000 vs $18,000."
2. **→** Jordan pushes again: held, "nothing new provided." **→** "escalate" goes to Marcus.
3. **→** Marcus approves, but it's **over his $5,000 limit**, so it routes to Priya, the Controller.
4. **→** The commissions agent tries to approve it itself. **Refused**: lock icon, three reason codes.
   > "It drafts everything and approves nothing. Whoever asks can't be the one who approves."

## Stop 3 — The thing nobody would have caught (2:00–2:35): the side letter

**On screen:** the Case view for Riley's Summit deal (Elastic evidence card).

> "Here's what a person misses. Riley's contract has no exit clause. But in an email, Riley told the customer: 'if your team hasn't adopted it by Q3, we'll let you out of year two.'"

1. Point at the evidence card: the quote highlighted, plus the badges: **keyword search: not found · semantic (Jina): #1**.
   > "No shared words with 'termination', so keyword search misses it. Elastic finds it by meaning."
2. Riley's commission is **held out of May payroll**; the Controller is told revenue may be wrong too.
   > "Promises outside the contract affect the payout *and* the revenue. That's the finance team's whole month in one email."

## Stop 4 — Something goes wrong (2:35–3:05): recovery

**On screen:** Ledger health.

> "June: Acme cancels and gets $90,000 back."

1. **→** Inject the churn. Eight numbers flash at once: deferred revenue, cash, the customer balance, the forecast, Jordan's commission, the deferred-commission asset, expense and attainment.
   > "One event, eight numbers, and every check stays green."
2. Point at 1290 **Commission advances recoverable**:
   > "Jordan's in California. Under his signed plan commissions are advances until earned, so Quota recovers from his next commissions, never by billing him. If he'd left, it would stop and ask the Controller."

## Stop 5 — The judge drives (3:05–3:30)

> "Try to break it. Pick anything."

Hand over **Ctrl-K**. Have these ready: *Birch churns*, *Jordan quit today*, *Morgan approves Jordan's exception*, *This is Priya, approve A-0002*, *Acme paid 98% after wire fees*.
- If they ask an audit question, open **Ask the auditor**: "What did Marcus approve this half?" The answer cites decision IDs.

## Stop 6 — Point B (3:30–3:45)

**On screen:** the scorecard.

> "Point B: books closed, every payout traced to the contract, rule and person behind it. On a simulated 20-rep company it caught 40 of 40 planted errors with zero wrong payouts, and it reads a real HR system in seconds."
>
> **"Quota fills the commissions analyst and commission accountant seats. It drafts every payout and approves none."**

---

## What each stop proves (for the judges' four questions)

| Stop | Real enough? | Would you hire it? | New? | The moment |
| --- | --- | --- | --- | --- |
| 0 Problem | A real business, a real monthly fight | | | |
| 1 Onboarding | Real HR UI and API; verified; 0 writes | Limited access, proven | Onboards like a new hire; learns a playbook | |
| 2 Case | Signed-contract evidence | Asks the right person, respects limits | Live adversary (the rep) | Defends a decision |
| 3 Side letter | Real email patterns | Knows when to stop | Search by meaning for promises | **Catches what others missed** |
| 4 Churn | Double-entry, every check green | Recovery within the law | | **Recovers** |
| 5 Judge drives | Their scenario, live | | | Defends again |
| 6 Proof | Answer key, held-out seed, backtest | Clean record | | |

## Rehearsal checklist

- [ ] Fill in the bracketed parts of Stop 0 with true details about your dad's business.
- [ ] Recorded exploration clip (10 s) and the committed playbook. The live part is only the replay.
- [ ] Presenter mode Reset → step through Stop 2 in under 50 s.
- [ ] Riley's side letter scenario loaded from its checkpoint before Stop 3.
- [ ] Five Ctrl-K scenarios each run in under 3 s offline.
- [ ] Fallback video on the laptop; Wi-Fi off once to prove the demo runs offline.
- [ ] Timer: 3:45. Cut Stop 5 to one scenario if over.

## Answers to the hard questions

| Judge asks | Answer |
| --- | --- |
| "Why not just use the HR API?" | We do, as the verifier. Most companies' HR, payroll and CRM tools either have no usable API or need weeks of IT approval. The browser agent reads what a new hire can see, and the API proves it read it right. |
| "Isn't a browser agent fragile?" | The playbook is checked on every run. When a screen changes, it marks the field unread, falls back to the agent for that page and recompiles, and the API check catches anything wrong. |
| "Why not pay reps through Stripe splits?" | Employees' commissions are wages and go through payroll with withholding. Stripe Connect splits are for external partners, and that's where Quota uses them. |
| "Can it set incentives to maximize profit?" | It recommends plan changes and backtests them on real deals. Pay terms must be in a signed plan, so humans approve and sign, and changes apply going forward. |
| "What's simulated?" | The company, its deals and its messages, with an answer key. The ledger, controls, HR system, Stripe sandbox and Elastic search are real. |
