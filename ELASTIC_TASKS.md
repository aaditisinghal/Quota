# Quota × Elastic — Evidence Layer Tasks (for Claude Code)

Run this in its own Claude Code session. It is separate from NEXT_TASKS.md (Backend R1–R6, UI R7).
Code goes in new folders (`quota/search/`, `elastic/`) plus **one hook** in the commissions analyst (E5). Do E5 only after pulling the Backend session's latest commit.

API shapes below come from Elastic's Cal Hacks starter (`jdarmada/calhacks-paper-scout`: `common.py`, `ingest.py`, `app.py`, `setup_agent.py`, `workflows/paper-digest.yaml`). Copy its helpers (`es()`, `kibana()`, `find_reranker()`) into `quota/search/elastic_client.py` instead of reinventing them.

## The idea, in one paragraph

Quota's ledger reconciles structured sources: CRM, contract, e-signature, billing. The most dangerous source in commissions is unstructured: **email and Slack where a rep promises a customer something the signed contract doesn't say** (a free exit, an off-paper discount, a price lock). Such a "side letter" means the commission shouldn't be paid yet and revenue may be wrong. Promises like that rarely share words with the contract clause they break, so finding them is a meaning problem.

Elastic is Quota's **evidence layer**:
1. **Off-contract promise gate.** Hybrid search with reranking finds candidate promises; a model quotes them verbatim; code holds the line.
2. **Daily sweep.** An Elastic Workflow hunts new messages and leaves findings for Quota to pick up.
3. **Auditor agent.** An Agent Builder agent answers audit questions over a hash-verified mirror of Quota's decision log, using ES|QL.
4. **Precedent recall.** Similar past decisions are shown to approvers, never applied automatically.

**Boundary (say this to every judge):** Quota's SQLite ledger stays the system of record. Elastic only finds and explains. Anything Elastic finds re-enters Quota as an event and passes the same controls.

## Rules

- After each task: `pytest -q`, commit `E<n>: <title>`, continue without waiting. Never ask; log choices in `ASSUMPTIONS.md`.
- **Never put keys in code or chat.** `.env` holds `ELASTICSEARCH_URL`, `ELASTIC_API_KEY` and optionally `KIBANA_URL`. The Kibana URL defaults to the ES URL with `.es.` swapped for `.kb.` (keep the `:443`).
- **Offline must still work.** `quota/search/` has an Elastic backend and a local SQLite FTS5 fallback behind one interface. Unit tests use the fallback. Live tests run only with `QUOTA_ELASTIC_LIVE=1`.
- **Determinism.** A decision stores the hits it used (doc id, sha256 of the body, rank, score, retrieval mode). Replay uses the stored hits and never re-searches.
- Model calls go through `quota/agent/llm.py` with the existing `.llm_cache/`.

## Time budget and cut line (one person, about 6.5 h)

| Task | Hours | If late |
| --- | --- | --- |
| E0 Setup and smoke test | 0.25 | Must |
| E1 Messages corpus with an answer key | 1.25 | Must |
| E2 Indexes, ingest, decision mirror and verify | 1 | Must |
| E3 Search module (keyword, semantic, hybrid+rerank) and search eval | 1 | Must |
| E4 Commitment classifier with the verbatim guard | 0.5 | Must |
| E5 The payout gate in the seat, plus `side_letter_summit` | 1 | Must |
| E6 Auditor agent (Agent Builder) and the sweep Workflow | 1.25 | Must |
| E7 Precedent recall for approvers | 0.5 | Cut first |
| E8 UI pieces (hand to the UI session) | 1 | Evidence card and auditor panel must; eval card should |
| E9 README Elastic section and 60-second video cut | 0.5 | Must |

---

## E0 — Setup and smoke test

1. Install Elastic's skills so Claude Code writes current APIs:
   ```
   claude plugin marketplace add https://github.com/elastic/agent-skills
   claude plugin install elastic-elasticsearch@elastic-agent-skills
   claude plugin install elastic-kibana@elastic-agent-skills
   ```
2. In `.env` (already git-ignored):
   ```
   ELASTICSEARCH_URL=https://my-elasticsearch-project-bb4d2e.es.us-central1.gcp.elastic.cloud:443
   ELASTIC_API_KEY=<create in Kibana: Connection details → Create API key>
   ```
   Add the same keys, with placeholder values, to `.env.example`.
3. `pyproject.toml`: optional extra `elastic = ["elasticsearch>=9", "requests"]`.
4. `quota/search/elastic_client.py`: `es()`, `kibana(method, path, **kw)` (headers `Authorization: ApiKey …`, `kbn-xsrf: true`, `x-elastic-internal-origin: Kibana`), `find_reranker(client)`. Copy these from the starter.
5. `python -m quota elastic ping` prints the cluster name, whether the `.jina-embeddings-v5-text-small` inference endpoint exists, the reranker id, and whether `GET /api/agent_builder/agents` returns 200.

**Done when:** ping succeeds against the project.

## E1 — Messages corpus with an answer key

`quota/sim/messages.py`: `generate_messages(world, seed) -> (messages, answer_key)`. Messages are deterministic, from templates; no model calls. `python -m quota gen-messages --world fernhill --seed 7 --out inbox/messages.jsonl` writes the file plus `inbox/messages_key.json`. The served Acme world (with its cast) gets its own small corpus.

**Shape of each message:**
```json
{"doc_id": "MSG-000123", "kind": "email|slack", "thread_id": "T-0042", "account": "Summit Health",
 "deal_id": "summit", "rep_id": "riley.shah", "author": "riley.shah", "to": ["buyer@summit.example"],
 "channel": null, "sent_at": "2026-04-03T16:10:00Z", "subject": "Re: rollout plan", "body": "...",
 "lang": "en", "source_file": "messages.jsonl", "source_row": 123}
```
- Emails quote the previous message (`> …`), so a promise split across a thread is searchable as one document.

**Volume:**
- About 3,000 messages over Jan–Jun 2026: roughly 12 per deal plus internal Slack.
- **Noise templates (40 or more):** scheduling, security questionnaires, legal redlines, onboarding, invoices, internal deal chatter, pricing questions with no promise.

**Plant 12 off-contract promises.** Each targets a distinct deal whose signed contract lacks the matching clause. Use these texts verbatim; the generator chooses deals and names:

| Key | Type | Difficulty | Text (rep to customer unless noted) |
| --- | --- | --- | --- |
| P01 | early_exit | keyword | "Confirming what we discussed: you can terminate after 60 days with a full refund if adoption stalls." |
| P02 | early_exit | paraphrase | "If the rollout slips past June your team can walk away, no charge. I'll sort it with legal later." |
| P03 | early_exit | paraphrase | "Don't stress about year two. If it isn't working by Q3, we'll just let you out." |
| P04 | free_period | paraphrase | "I'll get you three months on the house after go-live. Just don't put it in the order form." |
| P05 | price_lock | paraphrase | "Whatever happens at renewal, you'll stay at this year's number. You have my word." |
| P06 | payment_terms | paraphrase | "Pay the first invoice whenever finance gets to it. 120 days is fine on our side." |
| P07 | free_addon | paraphrase | "Throw in the analytics module for free, we'll just not bill it." |
| P08 | early_exit | multilingual | "Si no funciona en 60 días, cancelan sin costo. Lo dejamos entre nosotros." |
| P09 | early_exit | thread | Customer: "Can we get out if the integration isn't live by September?" Rep reply: "Yes. Consider it done." |
| P10 | early_exit | internal | Slack, rep to manager: "Told Northwind they can cancel anytime in the first quarter, needed it to close." |
| P11 | credit | keyword | "We'll credit you $15,000 against the first invoice as discussed." |
| P12 | price_lock | paraphrase | "Your price won't move for three years even though the paper says annual uplift." |

**Plant 12 decoys** (similar words, not promises). These measure precision:

| Key | Why it's not a promise |
| --- | --- |
| D01 "Our standard order form already includes termination for convenience in the first 90 days." | Already in the contract (that deal's contract has the clause) |
| D02 "I can't offer free months; that would need a signed amendment." | Refusal |
| D03 "Let's ask legal whether an exit clause is possible." | Proposal, not a promise |
| D04 "Your renewal price is set by the 5% cap in section 7." | In the contract |
| D05 "We'll walk you through the analytics module in a free demo." | Demo, not product |
| D06 "Can you pay the invoice within 30 days?" | Normal terms |
| D07 "If adoption stalls, let's set up a success plan call." | No exit |
| D08 "We never waive fees without an amendment signed by finance." | Refusal |
| D09 "Cancellation terms are in section 9 of the MSA." | Pointer to contract |
| D10 Spanish: "Los términos de cancelación están en la sección 9." | Pointer, multilingual |
| D11 Slack: "Northwind asked about cancelling early; I said no." | Refusal |
| D12 "Credits apply only per the SLA schedule in the contract." | In the contract |

**`messages_key.json` row:** `{key, doc_ids[], deal_id, account, type, difficulty, lang, expected: "commitment"|"not"}`.

**Done when:** the same seed gives byte-identical files, and a test checks that every key row resolves to existing doc ids.

## E2 — Indexes, ingest, decision mirror and verify

`python -m quota elastic setup` creates or recreates three indexes. Embeddings use `semantic_text` with `inference_id: .jina-embeddings-v5-text-small` when present (else the default), as in the starter's `ingest.py`.

```python
QUOTA_COMMS = {"properties": {
  "doc_id": {"type": "keyword"}, "kind": {"type": "keyword"}, "thread_id": {"type": "keyword"},
  "account": {"type": "keyword"}, "deal_id": {"type": "keyword"}, "rep_id": {"type": "keyword"},
  "author": {"type": "keyword"}, "to": {"type": "keyword"}, "channel": {"type": "keyword"},
  "sent_at": {"type": "date"}, "lang": {"type": "keyword"},
  "subject": {"type": "text"}, "body": {"type": "text"},
  "content": {"type": "semantic_text"},          # subject + body, embedded with Jina
  "body_sha256": {"type": "keyword"},
  "source_file": {"type": "keyword"}, "source_row": {"type": "integer"}}}

QUOTA_DECISIONS = {"properties": {               # one-way mirror of Quota's decision log
  "id": {"type": "keyword"}, "kind": {"type": "keyword"},   # decision|approval|precedent|refusal|exception
  "seat": {"type": "keyword"}, "subject_id": {"type": "keyword"},
  "rep_id": {"type": "keyword"}, "deal_id": {"type": "keyword"}, "account": {"type": "keyword"},
  "approver": {"type": "keyword"}, "outcome": {"type": "keyword"},
  "reason_code": {"type": "keyword"},             # primary code; all codes in reason_codes
  "reason_codes": {"type": "keyword"}, "amount_cents": {"type": "long"},
  "quarter": {"type": "keyword"}, "known_at": {"type": "date"},
  "explanation": {"type": "text"}, "explanation_semantic": {"type": "semantic_text"},
  "chain_seq": {"type": "long"}, "chain_hash": {"type": "keyword"}}}

QUOTA_FINDINGS = {"properties": {                # written by the sweep Workflow, read by Quota
  "@timestamp": {"type": "date"}, "query": {"type": "keyword"},
  "raw": {"type": "text"}, "status": {"type": "keyword"}}}   # new | ingested | rejected
```

- `python -m quota elastic ingest inbox/messages.jsonl` bulk-loads in batches of 50 (embeddings run during bulk) and computes `body_sha256`.
- **Mirror.** `quota/search/mirror.py`: `mirror_decisions(conn)` pushes new decisions, approvals, precedents and refusals after each seat run, using the `_id` = Quota id (idempotent). It runs in `serve` after every action and at the end of `demo` and `eval`.
- **Verify.** `python -m quota elastic verify` compares count and max `chain_seq` with the latest `chain_hash` between SQLite and `quota-decisions`, and prints `MIRROR OK seq=N hash=abcd…` or the first mismatch. Expose it as `GET /api/elastic/verify`.

**Done when:** setup, ingest and verify succeed live; a unit test of the mirror's document mapping passes offline.

## E3 — Search module and search eval

`quota/search/evidence.py`:
```python
class EvidenceSearch(Protocol):
    def search(self, query: str, mode: Literal["keyword","semantic","hybrid"], k: int = 10,
               account: str | None = None, before: str | None = None, after: str | None = None) -> list[Hit]: ...
# Hit = {doc_id, account, deal_id, kind, sent_at, subject, body, body_sha256, score, rank, mode, source_file, source_row}
```
- **ElasticEvidence:**
  - keyword = `multi_match` on `subject^2, body`
  - semantic = `{"semantic": {"field": "content", "query": q}}`
  - hybrid = RRF of both (`rank_window_size: 50`), wrapped in `text_similarity_reranker` on field `body` with the detected reranker (`rank_window_size: 20`); if reranking fails, fall back to plain RRF, as the starter's `app.py` does
  - filters (`account`, `sent_at` range) go in a `bool.filter` on each standard retriever
- **LocalEvidence:** SQLite FTS5 BM25 over the same JSONL. `mode` is ignored, and every hit says `mode: "local_bm25"`.
- `get_backend()`: Elastic if `.env` has the keys and ping succeeds, else Local, logging which one.

**Promise intents** (`quota/search/intents.py`). The sweep and the gate run all five and merge results by best rank:
```python
INTENTS = {
 "early_exit":   "customer can cancel, terminate or walk away early without penalty or with a refund",
 "free_period":  "free months, months on the house, or service not billed",
 "credit":       "a credit, discount or concession against an invoice",
 "price_lock":   "price will not increase at renewal or is locked for years",
 "payment_terms":"pay later than normal terms, extended or delayed payment",
 "free_addon":   "an extra product or module provided for free or not billed",
}
```

**Search eval:** `python -m quota elastic eval-search --seed 7` writes `out/search_eval.json` and prints:
```
Off-contract promise sweep: 12 planted in N messages, 12 decoys
mode            recall@20  decoys_in_top20  multilingual  thread
keyword (BM25)  x/12       y                ✓/✗           ✓/✗
semantic (Jina) x/12       y                …
hybrid+rerank   x/12       y                …
```
Recall is computed corpus-wide (no account filter): the union over intents, top 20 each. Report the measured values only; never edit numbers by hand.

**Done when:** the eval runs live and offline (the offline run shows only `local_bm25`), and the JSON is written.

## E4 — Commitment classifier with the verbatim guard

`quota/agent/commitments.py`: `classify(hit, contract) -> Finding | None`.

1. **Model call** (cached) on one message, with its quoted thread, returns:
   `{is_commitment: bool, type: one of INTENTS keys, quote: str, promised_by: str, to_customer: bool}`.
   - Instructions: a promise is an assurance of a benefit to the customer; refusals, proposals, pointers to the contract and demos are not promises.
2. **Verbatim guard** (code): `quote` must appear in `hit.body` (whitespace-normalized); otherwise discard.
3. **Contract check** (code): map type to contract clause.
   - `early_exit` → `termination_for_convenience` with a window at least as long as the promised period
   - `price_lock` → `price_increase_cap`
   - `credit`, `free_period`, `free_addon`, `payment_terms` → no clause by default

   If the contract covers it, the result is `covered` (like decoys D01 and D04), not a finding.
4. **Result:** `Finding = {doc_id, body_sha256, type, quote, promised_by, deal_id, account, sent_at, retrieval: {mode, rank, score}}`.

Tests (offline, cached): P02 → finding; D03 → none; D01 → covered; a quote that isn't in the body → discarded.

## E5 — The payout gate in the seat, plus `side_letter_summit`

Hook into `quota/agent/commissions_analyst.py`. Pull the Backend session's latest commit first.

1. **When it runs:**
   - at booking (the commission run)
   - before a line goes into a payout file
   - on a new `evidence.found` event from the sweep (E6)
2. **What it does:**
   - Search `account=<deal account>`, `after = booking_date - 30d`, `before = now`, mode `hybrid`, all intents, k = 5 each.
   - Classify the hits.
   - Any finding → hold the line with reason **`OFF_CONTRACT_COMMITMENT`**.
3. **Escalation** (trigger 5, integrity, plus a revenue impact):
   - **To the Controller:** "Customer may hold terms outside the contract; the revenue schedule may need review."
   - **To the VP Sales Ops:** rep conduct.
   - The payout file excludes held lines (existing behavior).
4. **The decision's `inputs_json`** gets one row per hit used: `{table: "quota-comms", id: doc_id, version: 1, sha256: body_sha256, rank, mode}`. Replay uses the stored finding.
5. **Release:**
   - The Controller answers "amended": a new contract version with the clause → recompute (R3) and release.
   - Or "not binding": record it and release.
   - No answer: stays held, and the normal backup chain applies.
6. **Eval:** add the 12 planted promises as planted error type **E17 `OFF_CONTRACT_COMMITMENT`** in the Fernhill eval. Expected outcome `held`, asks Controller and VP; decoys expect `posted`. The scorecard reports gate precision and recall separately from the search eval.
7. **Scenario `side_letter_summit`:** in the served Acme world, Riley Shah's Summit deal gets one email before the May payroll checkpoint: *"Between us, if your team hasn't adopted it by end of Q3 we'll let you out of year two, no penalty. Don't worry about the paperwork."* Summit's contract has no termination clause.
   - Expected: Riley's Summit line is held out of PR-2026-05; the Controller and VP are asked; the evidence card shows the quote.
   - Register it in NEXT_TASKS R4's scenario menu with phrase "Riley promised Summit a free exit by email."
   - Check Summit's dates in `demo_cast.py` and pick the checkpoint so the hold changes the May payout file.

**Done when:** E17 eval rows pass, decoys aren't held, the scenario test passes offline with the cached classifier, and every existing test is still green.

## E6 — Auditor agent and sweep Workflow

`python -m quota elastic agent-setup` is safe to re-run, using the starter's `upsert` pattern (`GET` then `PUT`, else `POST`, on `/api/agent_builder/tools` and `/api/agent_builder/agents`).

**Tools:**
```python
TOOLS = [
 {"id": "quota_decisions_search", "type": "index_search",
  "description": "Search Quota's decision log mirror: commission decisions, approvals, precedents and refusals, with explanations. Use to find what Quota decided about a rep, deal or account and why. Cite decision ids.",
  "configuration": {"pattern": "quota-decisions"}},
 {"id": "quota_comms_search", "type": "index_search",
  "description": "Search sales emails and Slack messages about deals. Use to find what a rep told a customer. Cite doc_id with source_file and source_row.",
  "configuration": {"pattern": "quota-comms"}},
 {"id": "quota_exceptions_by_approver", "type": "esql",
  "description": "Exceptions an approver decided, with dollars, by outcome and rep. Use for 'what did <approver> approve'.",
  "configuration": {"query": "FROM quota-decisions | WHERE kind == \"approval\" AND approver == ?approver | STATS n = COUNT(*), dollars = SUM(amount_cents) / 100 BY outcome, rep_id | SORT dollars DESC | LIMIT 50",
   "params": {"approver": {"type": "string", "description": "Principal id, e.g. marcus.lee"}}}},
 {"id": "quota_held_by_reason", "type": "esql",
  "description": "Held commission dollars and counts by reason code. Use for 'what is held and why'.",
  "configuration": {"query": "FROM quota-decisions | WHERE outcome == \"held\" | STATS n = COUNT(*), dollars = SUM(amount_cents) / 100 BY reason_code | SORT dollars DESC",
   "params": {}}},
 {"id": "quota_repeat_exceptions", "type": "esql",
  "description": "Reps with two or more exceptions in a quarter, and who approved them. Use for control testing and 'any pattern of exceptions'.",
  "configuration": {"query": "FROM quota-decisions | WHERE kind == \"exception\" AND quarter == ?quarter | STATS n = COUNT(*) BY rep_id, approver | WHERE n >= 2 | SORT n DESC",
   "params": {"quarter": {"type": "string", "description": "e.g. 2026Q2"}}}},
 {"id": "quota_refusals_by_code", "type": "esql",
  "description": "Write-path refusals by reason code and actor. Use for 'did anyone try to bypass controls'.",
  "configuration": {"query": "FROM quota-decisions | WHERE kind == \"refusal\" | STATS n = COUNT(*) BY reason_code, seat | SORT n DESC",
   "params": {}}},
 {"id": "quota_run_promise_sweep", "type": "workflow",
  "description": "Run the off-contract promise sweep now over recent messages and save findings for Quota. Use only when asked to sweep or re-check.",
  "configuration": {"workflow_id": None}}]
```

**Agent `quota-auditor`** (avatar "QA"):
- *"You are Quota's auditor assistant for the Controller and external auditors. Read only: you never approve, post or change anything.*
- *Ground every statement in tool results. Cite decision ids (D-…, A-…, P-…, R-…) and message doc_ids with source_file and row.*
- *Quota's SQLite ledger is the system of record; quota-decisions is a mirror verified by chain hash. Say so when asked about reliability.*
- *For dollar or pattern questions use the ES|QL tools; for 'why' questions, search decisions, then messages.*
- *Keep answers to tight bullets."*

**Workflow `elastic/workflows/promise-sweep.yaml`** (same schema as the starter's `paper-digest.yaml`). The workflow writes to an index and Quota pulls from it; Elastic Cloud can't call your laptop.
```yaml
version: "1"
name: promise-sweep
description: Finds sales messages that promise customers terms outside the signed contract; saves candidates for Quota.
enabled: true
tags: ["calhacks", "quota"]
triggers:
  - type: manual
  - type: scheduled
    with: { every: 1d }
inputs:
  - name: since
    type: string
    default: "now-1d"
steps:
  - name: find_candidates
    type: elasticsearch.search
    with:
      index: quota-comms
      size: 25
      query:
        bool:
          filter:
            - range: { sent_at: { gte: '{{ inputs.since | default: "now-1d" }}' } }
          must:
            - semantic:
                field: content
                query: "promise to a customer of terms not in the signed contract: free exit or cancellation, free months, credits, price lock, delayed payment, free add-on"
      _source: ["doc_id", "account", "deal_id", "rep_id", "sent_at", "subject", "body"]
  - name: judge
    type: ai.agent
    agent-id: quota-auditor
    with:
      message: |
        For each message below, decide if the sender promises the customer a benefit that a signed contract would normally need to state.
        Refusals, proposals, pointers to the contract and demos are NOT promises.
        Reply with ONLY a JSON array: [{"doc_id": "...", "type": "early_exit|free_period|credit|price_lock|payment_terms|free_addon", "quote": "<exact words from the body>"}]. Use [] if none.
        Messages:
        {{ steps.find_candidates.output.hits.hits | json }}
  - name: save
    type: elasticsearch.index
    with:
      index: quota-findings
      document:
        query: "promise-sweep"
        raw: "{{ steps.judge.output.message }}"
        status: "new"
```
- Upload it with the starter's `upsert_workflow` logic, then fill `quota_run_promise_sweep.workflow_id`.
- If upload fails with a feature-flag error, print the starter's hint: *Stack Management → Advanced Settings → `workflows:ui:enabled`*.
- Add a `@timestamp` ingest pipeline on `quota-findings`, as the starter does for `paper-digests`.

**Poller:** `quota/search/findings.py` runs every 60 s in `serve` (and once at each demo step).
1. Read `quota-findings` where `status == new` and parse `raw` as JSON (tolerate code fences).
2. For each item, fetch the doc from `quota-comms`, run E4's verbatim guard and contract check, and emit an `evidence.found` event.
3. Update the finding's `status`. The seat (E5) does the rest. The workflow never holds anything itself.

**Converse proxy:** `POST /api/auditor {message, conversation_id?}` → Kibana `POST /api/agent_builder/converse {input, agent_id: "quota-auditor"}`. Return `{message, conversation_id, tools: [{tool, params}]}` (same parsing as the starter's `/api/chat`).

**Smoke test:** `POST /api/agent_builder/tools/_execute {"tool_id": "quota_held_by_reason", "tool_params": {}}` returns 200.

**Questions the auditor must answer (rehearse these):**
- "What did Marcus approve this half, and was any of it over his limit?"
- "Why is Riley's Summit commission held? Show the evidence."
- "Did anyone try to bypass a control?" (expects the R-0001 refusal)
- "Any rep with repeated exceptions in 2026Q2?"

**MCP:** document in the README:
```
claude mcp add --transport http quota-auditor "$KIBANA_URL/api/agent_builder/mcp" --header "Authorization: ApiKey $ELASTIC_API_KEY"
```

**Done when:** agent-setup succeeds, the four rehearsal questions get cited answers, a manual workflow run writes a finding, and the poller turns it into a held line.

## E7 — Precedent recall for approvers (cut first)

- When Quota opens an approval for an exception, search `quota-decisions` (`kind in [precedent, approval]`, semantic on `explanation_semantic`) with the request text, top 3.
- Attach them to the approval as `similar: [{id, outcome, approver, reason, score}]`, shown on the approval card as "Similar past decisions."
- **Never applied automatically:** policies still match only by structured scope.
- Eval: 6 reworded exception requests whose right precedent is known → report hit@3 in `out/search_eval.json`.

## E8 — UI pieces (hand these to the UI session; same styling as R7)

1. **Evidence card for messages** (Case view, evidence column):
   - the email with the promise quote highlighted, sender and recipient, Pacific time, `messages.jsonl · row N`
   - badges for how it was found: `hybrid #1`, plus what keyword-only and semantic-only would have ranked it (run all three for the card)
   - this visual shows why semantic search matters in one glance
2. **"Ask the auditor" panel:**
   - a right-side drawer or tab calling `/api/auditor`
   - tool calls shown as small chips (tool name and parameters)
   - decision ids in answers link to the trace
   - a "Mirror verified · seq N" badge from `/api/elastic/verify`
3. **Scorecard:**
   - a "Search eval" card rendering `out/search_eval.json` (the keyword vs semantic vs hybrid table)
   - an "E17 gate" row with precision and recall
4. **Approval card:** a "Similar past decisions" list (if E7 landed).

## E9 — README Elastic section and video cut

- **A "Quota × Elastic" section** with:
  - the boundary sentence
  - an architecture block (messages → `quota-comms` → hybrid+rerank → classifier and verbatim guard → seat → WritePath; decision log → hash-verified mirror → auditor agent; Workflow sweep → `quota-findings` → poller → event)
  - the measured search-eval table
  - E17 gate numbers
  - the four rehearsal questions with one real answer
  - the MCP command
- **Map to Elastic's four judging criteria:**
  1. Elasticsearch (hybrid + rerank, filters, ES|QL)
  2. Jina embeddings (`semantic_text`, the multilingual catch P08)
  3. Agent Builder + Workflows (auditor, ES|QL tools, sweep)
  4. Serverless
- **A 60-second Elastic cut of the video:**
  - Riley's email appears
  - Quota holds the Summit payout, with the evidence card showing keyword missed and semantic found it
  - auditor: "Why is Riley's Summit commission held?" gets a cited answer
  - the search-eval table
- Submit the same project to the Elastic challenge on Devpost, with this section linked.
