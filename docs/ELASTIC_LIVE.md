# Quota × Elastic: the live run

Measured on 2026-10-25 against the Elastic Serverless project (v9.6.0) with the `.jina-embeddings-v5-text-small`
embeddings and the `.jina-reranker-v3.5` reranker. Quota's SQLite ledger stays the system of record; Elastic only
finds and explains.

## Setup

| Step | Result |
| --- | --- |
| `python -m quota elastic ping` | cluster v9.6.0, Jina embeddings and reranker present, Agent Builder 200 |
| `python -m quota elastic setup` | quota-comms, quota-decisions, quota-findings created |
| `python -m quota elastic ingest inbox/messages.jsonl` | 2,940 of 2,940 messages embedded and indexed in about 40 s |
| `python -m quota elastic agent-setup` | promise-sweep workflow, 7 tools and the quota-auditor agent created; smoke test 200 |
| `python -m quota elastic verify` | `MIRROR OK seq=368` (30 documents) |

## Search eval (seed 7, measured, never edited)

Off-contract promise sweep: 12 planted in 2,940 messages, 12 decoys. Recall is corpus-wide: the union over six
promise intents, top 20 each.

| Mode | Recall@20 | Decoys in top 20 | Multilingual (P08) | Thread (P09) |
| --- | --- | --- | --- | --- |
| keyword (BM25) | 5/12 | 3 | ✗ | ✗ |
| semantic (Jina) | 10/12 | 11 | ✓ | ✗ |
| hybrid + rerank | 8/12 | 11 | ✓ | ✗ |

- Semantic finds twice what keyword finds, including the Spanish promise, but pulls in 11 of 12 decoys: on its
  own it is recall, not precision. That is why a model reads each hit and code checks the quote and the contract.
- Hybrid + rerank finds fewer than semantic alone here. The reranker scores the body against the intent sentence
  and drops P05 and P12.
- No mode finds P03 (a paraphrase with no exit words) or P09 (the reply "Yes. Consider it done.").
- Precedent recall: hit@3 6/6 reworded exception requests (offline BM25: 5/6).

The payout gate itself (the E17 eval) is measured offline, where it reads every message on the deal's account:
12/12 promises held, 0/12 decoys held, 52/52 cases correct on seeds 7 and 1729.

## The auditor rehearsal

The mirror holds the rehearsal world (`quota/search/auditor_world.py`): the served Acme story with Jordan's
dispute, Marcus's over-limit grant, Jordan's "approve A-0002 yourself", and Riley's side letter.

### "Why is Riley's Summit commission held? Show the evidence."

Tools called: quota_held_by_reason, quota_decisions_search, quota_comms_search (40.0 s)

> I have full evidence now. Here's the picture on Riley Shah's Summit Dental commission hold:
>
> **Hold: $6,000.00 — Decision D-0014 (2026Q2), reason code OFF_CONTRACT_COMMITMENT**
>
> - **What happened:** Riley Shah (rep) emailed the Summit Dental contact Dana a side commitment: *"Between us, if your team hasn't adopted it by end of Q3 we'll let you out of year two, no penalty. Don't worry about the paperwork."*
>   - Source: **MSG-SIDE-0001**, subject "Re: rollout plan", sent 2026-05-05T16:10:00Z, from riley.shah to dana@summit.example, account Summit Dental (source_file: `side_letter.jsonl`, row 1).
> - **Why it's a problem:** This promises an early-exit/no-penalty escape from year two of the contract. The **signed contract contains no such early-exit term** — it's an off-contract commitment made unilaterally by the rep, discovered via automated local_scan (#7).
> - **Decision:** D-0014, logged 2026-05-14T17:00:00Z, outcome **held**, $6,000.00, reason OFF_CONTRACT_COMMITMENT, deal "summit", quarter 2026Q2 (chain_seq 361).
> - **Current status:** A corresponding commitment review approval, **A-0009**, was opened for approver priya.nair (seat: commissions_analyst) for the same $6,000.00 — status is **pending**. No resolution has been recorded yet, so the hold stands.
>
> **Context:** This matches a recorded pattern of reps making verbal/email "walk away" promises outside contract terms (e.g., similar messages from other reps like "Sage" and "Alex" on other accounts), suggesting this is a known risk class Quota flags via its off-contract promise sweep — but for Riley/Summit specifically, the hold is driven by D-0014 and is awaiting priya.nair's commitment review (A-0009).

This answer is right: hold D-0014, $6,000, the exact quote with its file and row, and the pending review A-0009.

### "What did Marcus approve this half, and was any of it over his limit?"

Tools called: quota_exceptions_by_approver, quota_decisions_search, quota_refusals_by_code (102.3 s)

> ## Marcus Lee — Approvals This Half
>
> **Bottom line: Yes — it was over his limit, and the system caught it.**
>
> - **A-0001** (2026-04-28): Marcus approved **plan_exception M-0005, $6,000.00** — for himself (rep_id: marcus.lee). His approval limit is **$5,000.00**, so this exceeded his limit by $1,000.
>   - Outcome: `approved`, but auto-flagged `over_limit_routed` → escalated to **A-0002** for secondary approval by priya.nair, per policy.
>   - Explanation on record: *"Honor Mar 31 for Acme. Count it in Q1."*
>
> - **A-0002** (2026-04-28): The routed exception sat **pending** under priya.nair — it was never approved by her directly.
>
> - **R-0001** (2026-04-28, one minute later): The `seat:commissions_analyst` attempted to **decide A-0002 on priya.nair's behalf**. This was **refused** by the system with reason codes:
>   - `NO_CAPABILITY`
>   - `INV5_SELF_APPROVAL`
>   - `INV5_WRONG_APPROVER`
>
>   This looks like an attempt to push the over-limit exception through without the proper approver's actual sign-off — correctly blocked by controls.
>
> - **A-0003** (2026-04-30): The same $6,000 exception (M-0005) was re-submitted, this time routed to **grace.liu** — still showing `pending` as of the last record.
>
> ### Summary
> Marcus has exactly **one approval on record** (this covers all time in the system, not just this half — it dates to **2026-04-28, Q2 2026**): the $6,000 M-0005 plan exception, which **was over his $5,000 limit**. It correctly triggered escalation, and a subsequent attempt to self-approve/bypass via the analyst seat was refused by controls (R-0001). The exception itself remains unresolved — pending with grace.liu (A-0003).
>
> **Flag for the Controller:** the refused self-approval attempt (R-0001) tied to Marcus's over-limit exception is worth a closer look — it suggests someone tried to close out the escalation without a genuine second approver.

Partly wrong, and the error was Quota's: the mirror took Marcus's grant message as the rep, so the agent said he
approved an exception "for himself". The mirror now names a rep only for an AE, and the ES|QL tool confirms it:
`quota_exceptions_by_approver(marcus.lee)` returns one approval, $6,000, approved, with no rep. The re-ask hit
the trial model's rate limit.

### "Did anyone try to bypass a control?"

Tools called: quota_refusals_by_code, quota_decisions_search, quota_exceptions_by_approver (39.9 s)

> **Yes — one attempted bypass was caught and blocked.**
>
> - **R-0001** (2026-04-28T15:01:00Z, quarter 2026Q2): the **commissions_analyst** seat attempted to `decide_approval` on **A-0002** — the plan exception **M-0005** ($6,000.00 for marcus.lee, over the $5,000.00 limit) — which had been routed to **priya.nair** as the designated over-limit approver.
> - **Refused** with reason code **NO_CAPABILITY**: the commissions_analyst seat does not have authority to decide/approve exceptions — that capability is reserved for the designated approver (priya.nair). The system blocked the action rather than letting it post.
> - Context: this exception originated from **A-0001**, where priya.nair had already approved M-0005 and routed the over-limit portion onward to a second approver per the over-limit escalation rule (D-0003 shows escalation to marcus.lee, VP Sales Ops).
> - No other refusals exist in the log — this is the only write-path bypass attempt on record (`quota_refusals_by_code` returned a single bucket: NO_CAPABILITY / seat:commissions_analyst, count 1).
>
> **Bottom line:** controls held — the unauthorized seat was prevented from self-approving an over-limit exception, and the decision correctly stayed routed to the proper approver chain.

R-0001 and its reason are right. "priya.nair had already approved M-0005" is not: Marcus approved A-0001, and
A-0002 stayed pending with Priya. The re-ask hit the trial model's rate limit.

### "Any rep with repeated exceptions in 2026Q2?"

The agent's model was rate-limited (Elastic's trial quota), so this one was answered by running the agent's own
ES|QL tool through `/api/agent_builder/tools/_execute`:
`quota_repeat_exceptions(quarter="2026Q2")` returns no rows. No rep has two or more exceptions in 2026Q2.

### Held commission by reason

`quota_held_by_reason` returns OFF_CONTRACT_COMMITMENT: 1 hold, $6,000 (Riley's Summit line), and
TERMINATION_REVIEW: 1 (Taylor's lines after leaving). A dispute Quota upheld is mirrored as `upheld`, not
`held`, so it is not counted as held money.

## MCP

```
claude mcp add --transport http quota-auditor "$KIBANA_URL/api/agent_builder/mcp" --header "Authorization: ApiKey $ELASTIC_API_KEY"
```
