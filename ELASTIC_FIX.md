# Elastic — make the demo path real (for Claude Code)

The Elastic pieces are real in setup, ingest, the search eval, precedent recall, the mirror and the auditor agent (see `docs/ELASTIC_LIVE.md`).

Three gaps between the plan and the code. Each looks hard-coded to a judge who reads it:
1. **The payout gate never calls Elastic in the served demo.** `api/session.py: attach_evidence()` always builds `LocalEvidence` and scans the whole account mailbox (`mode: local_scan`). The auditor's own answer said Summit was "discovered via automated local_scan (#7)".
2. **The promise reader is never the model.** `world.commitment_llm` is set to `None` in `sim/world.py` and assigned nowhere, so `commitments.verdict()` always uses `rule_verdict()`. Its regexes ("let you out", "on the house", "price won't move", "not bill it", "consider it done") are written around the 12 planted sentences, which makes E17's "12/12 held, 0 decoys" circular.
3. **The sweep loop isn't shown working.** Workflow → `quota-findings` → poller → `evidence.found` → hold has no measured live run. The workflow's default `since: now-1d` finds nothing in a world dated Jan–Jun 2026.

Do X1–X6 in order. Same rules as before: tests green, commit `X<n>: …`, log choices in ASSUMPTIONS.md, no keys in code.

## X1 — The gate searches Elastic when Elastic is configured

- `attach_evidence(world, extra)`:
  - use `quota.search.evidence.get_backend()`
  - when the backend is Elastic, first ingest the served Acme corpus (`messages.corpus_for("acme", 7)`) and `extra` into `quota-comms` (idempotent by `doc_id`, `refresh="wait_for"`)
  - keep `LocalEvidence` only as the fallback when Elastic isn't configured or doesn't answer
- Scenarios that add messages (`side_letter_summit`) index them through the same function before the gate runs.
- **Gate retrieval with Elastic:** take the union per intent of `semantic` top 5 and `hybrid` top 5, filtered to the deal's account and window. Semantic had the best recall in the measured eval (10/12), and hybrid adds keyword precision. `retrieval.mode` on each hit stays `semantic` or `hybrid_rerank`, never `local_scan`.
- The mailbox scan (`account_messages`) runs **only** with the local backend, and the decision text says `local_scan (offline)`.
- **Test:** with a fake Elastic client returning the side-letter doc, the Summit hold's decision inputs carry `mode in {semantic, hybrid_rerank}` and the search was called once per intent per mode.

## X2 — The model reads the hits; rules only offline

- In `acme_session(...)`, `eval.attach_gate(...)` and the scenario runner:
  - set `world.commitment_llm = llm` when an Anthropic key is configured (the same `quota/agent/llm.py` client, cached in `.llm_cache/`)
  - set it to `None` only when `QUOTA_OFFLINE=1` or there's no key
- Every finding already records `read_by`. Show it on the evidence card and in the decision text: `read by model` or `read by rules (offline)`.
- **Warm the cache** for the demo: run the served world and the E17 eval once with the key so `QUOTA_OFFLINE=1` replays real model verdicts from cache instead of falling back to regex.

## X3 — Honest E17 numbers, including a held-out set

1. Add `inbox/heldout_promises.jsonl`:
   - **12 new promises** and **12 new decoys** in wording not used by the generator or the regexes
   - write them by asking the model for paraphrases of each promise type with a fixed prompt and seed; commit the prompt with the file
   - check that none of them match `rule_verdict` patterns verbatim
   - insert them into the Fernhill corpus on other deals
2. `python -m quota eval --seed 7 --gate-report` prints one table, and writes it to `out/gate_eval.json`. Rows are reader × retrieval, columns are the two sets:

   | Reader × retrieval | Templated set (12 + 12) | Held-out set (12 + 12) |
   | --- | --- | --- |
   | rules × mailbox scan (offline) | held x/12, decoys held y/12 | … |
   | model × mailbox scan | … | … |
   | model × Elastic (semantic ∪ hybrid) | … | … |

3. README and ELASTIC_LIVE.md use these numbers. State plainly that the rule reader was written against the templated set, so the held-out column is the real test. Report what you measure; don't tune after seeing the held-out results.

## X4 — Run the sweep loop live and write it down

- Workflow input `since` defaults to a value the simulated world can hit. Pass it from Quota: `python -m quota elastic sweep --since 2026-04-01T00:00:00Z`. Keep the workflow default as `now-1d` for real deployments and note it.
- Give the workflow's `ai.agent` step its own small agent, `quota-sweeper`: no tools, instructions = the judge prompt only. It's faster, and it doesn't compete with the auditor's rate limit.
- **Live run:**
  1. serve with Elastic
  2. run the sweep
  3. the poller (`session.poll_findings`) turns the saved finding into `evidence.found`
  4. the gate holds the line

  Record the finding id, workflow execution id, timings, and the resulting decision and approval ids in a new "Sweep, live" section of `docs/ELASTIC_LIVE.md`.
- **Test** (fake store): a finding whose quote isn't in the message is rejected; a covered promise is rejected; a real one emits exactly one event.

## X5 — Make the auditor reliable on stage

- If Agent Builder lets the agent use your own model connector (Kibana → Stack Management → Connectors → Anthropic or another provider, then select it in the agent's model settings), configure `quota-auditor` to use it, to avoid the trial model's rate limit. Check whether that's available in this Serverless project before relying on it, and record the result.
- **UI fallback** in the "Ask the auditor" panel: if `converse` errors or takes over 45 s, show "Agent busy; running its tool directly", call `/api/agent_builder/tools/_execute` for the ES|QL tool matching the question (held by reason, exceptions by approver, refusals by code, repeat exceptions), and render the table with a link to each decision id.
- Re-run the four rehearsal questions after the mirror fix and replace the answers in ELASTIC_LIVE.md, keeping the honest notes on anything still wrong.
- Pre-record one good auditor answer as a 20-second clip for the video.

## X6 — Hybrid shouldn't lose to semantic (optional, 30 minutes max)

Hybrid plus rerank found 8/12 against semantic's 10/12, because the reranker scores `body` against the intent sentence and drops P05 and P12.
- Try: `rank_window_size: 50` on the reranker, and reranking on `subject + body`.
- Report the new row in `out/search_eval.json` whether it improves or not. Keep whichever the gate uses (X1) consistent with what's reported.

## Done when

- The served demo's Summit hold says `found by semantic #k` (or `hybrid_rerank`) and `read by model`.
- `out/gate_eval.json` has all six cells.
- ELASTIC_LIVE.md has a live sweep run.
- The auditor panel answers even when the agent is rate-limited.
