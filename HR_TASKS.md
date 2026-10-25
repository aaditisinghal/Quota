# Quota × HR — Onboarding Agent, People Dashboard, Contracts (for Claude Code)

Run this in its own Claude Code session (call it **HR**). Backend R1–R7 are done; the Elastic session owns `ELASTIC_TASKS.md`.
The HR clone lives at `C:\Users\aadit\Downloads\bamboohr-clone` (Frist's BambooHR replica: real UI, BambooHR-shaped API, `/__clone/*` environment API with reset, diff and ledger). You may edit it.

## The point of this work (read first)

The bottleneck for any autonomous finance system is **onboarding**: weeks of integrations before it can do anything. Quota onboards the way a new hire does:
1. It opens the company's HR system in a browser and learns the screens.
2. It reads every employee, their pay and their commission records.
3. It **proves** two things: every value it read matches the HR system's own API, and it changed nothing (the HR system's own action ledger shows zero writes).
4. It turns the first exploratory run into a playbook, so the next run takes seconds with no model calls.

That gives the demo its opening moment, and it makes "this works at any company today" believable.

## Rules

- Same as NEXT_TASKS.md: commit `H<n>: <title>` after each task, `pytest -q` green, never ask questions, log choices in `ASSUMPTIONS.md`.
- **Read-only by default.** The onboarding agent must not write to the HR system. Writes (H6) go through the HR API only, and only on approval.
- **Never need the network for the demo.** The exploratory run is recorded once, and its playbook and transcript are committed. The live demo runs the compiled playbook against the local clone.
- Honest naming: call it a **browser agent** (Claude choosing actions over the page's accessibility tree, executed by Playwright). Do not claim it was "trained"; it **learns a playbook from its own run**.

## Time budget (one session, about 5 h of must-haves)

| Task | Hours | Must or stretch |
| --- | --- | --- |
| H0 Run the clone; seed Fernhill's people into it | 0.75 | Must |
| H1 Browser agent: explore and extract (model-driven) | 1.5 | Must |
| H2 Compile the run into a playbook; replay with no model calls | 0.75 | Must |
| H3 Verify against the HR API; prove zero writes with the clone ledger | 0.5 | Must |
| H4 People dashboard in Quota (from HR data) | 1 | Must |
| H5 Quota's API contract and MCP server | 0.75 | Must |
| H6 Write paid commissions back to HR through its API, with proof | 0.5 | Stretch |
| H7 Partner splits through Stripe Connect (external partners only) | 1.25 | Stretch |
| H8 Incentive Lab: margin-aware plan recommendations, backtested, never auto-applied | 1.5 | Stretch |

**Hard checkpoint:** H0–H4 working by 4 am. Otherwise skip H5's MCP (keep the OpenAPI link) and all stretch tasks.

---

## H0 — Run the clone and seed Fernhill's people into it

1. Run the clone on port 8080 (`docker run -p 8080:8080 ghcr.io/runfrist/bamboohr-clone:v1`, or from source with `bun run setup && bun run build && bun run start`). Set `CLONE_ADMIN_TOKEN` in Quota's `.env` and the clone's environment.
2. **Fernhill overlay.** Add `app/backend/backend-generation-v1/src/seed/fernhill.mjs`, called at the end of `generate.mjs` when `WORLD=fernhill`.
   - A `Sales` department, plus `Finance` if missing.
   - Quota's cast with exact names and emails (`jordan.kim@fernhill.example` and so on): Jordan Kim (AE, CA, manager Morgan Diaz), Morgan Diaz (Sales manager), Marcus Lee (VP Sales Ops), Priya Nair (Controller), Grace Liu (CFO), Dana Ortiz (Payroll manager), Riley Shah, Alex Chen, Taylor Brooks (CA; termination date to match Quota's cast). Fill the rest of the Fernhill eval reps up to 20 AEs.
   - For each rep, `EmployeeTableRow` rows:
     - `compensation` (payRate, payPer: Year)
     - `commission`: historical Q1 payouts matching Quota's opening lines, e.g. Jordan's $10,000 / $12,000 / $8,000 dated in Q1
     - `jobInfo`: title, department, reports to
   - Base salaries: AEs $90k–$130k; keep Jordan at $110,000. Values are deterministic from the seed.
3. Quota side: `quota/hr/cast.py` lists what HR should contain (the answer key for H3). It's generated from the same cast definitions, so the two can't drift.
4. `python -m quota hr ping` checks `GET /api/v1/employees` and `GET /__clone/state` and prints the employee count.

**Done when:** a fresh clone with `WORLD=fernhill` shows Jordan Kim's Job tab with compensation and commission rows in the browser.

## H1 — Browser agent: explore and extract

`quota/hr/browser_agent.py` (Playwright, already installed with Quota's tooling; Claude through `quota/agent/llm.py`).

1. **Observation:** for the current page, build a compact list of interactive elements (role, accessible name, a stable id like `e17`), plus visible text truncated to 6,000 characters. No screenshots needed.
2. **Actions:** the model calls exactly one per step:
   - `goto(url)`
   - `click(element_id)`
   - `type(element_id, text)`
   - `extract(records: list[dict], source_url)`
   - `done(summary)`

   Every `extract` record must quote values that appear verbatim in the page text; otherwise it's rejected and the agent is told why. This is the same verbatim rule as Quota's extraction.
3. **Goal prompt:** "You are joining Fernhill's finance team. Using this HR system, list every active employee with name, work email, department, job title, manager, state, hire date, termination date, pay rate and period, and every commission row (date, amount). Read only: never click Save, Add, Edit, Delete, Request or Approve." A code-side guard also refuses clicks whose accessible name matches `/save|add|edit|delete|request|approve|submit|upload/i`, and logs a refusal.
4. **Budget:** at most 60 model steps in exploration mode. The agent is expected to discover the directory and the URL pattern of a profile's Job tab, then extract a few employees fully.
5. **Trajectory log:** `runs/hr/explore-<ts>.jsonl` with one line per step (observation hash, action, result, URL, time, tokens).
6. **CLI:** `python -m quota hr explore --base http://127.0.0.1:8080 --sample 3` prints steps, time and model calls, and writes the trajectory.

**Done when:** one exploratory run completes on the local clone and extracts 3 employees correctly. Commit its trajectory and the `.llm_cache` entries it used, so the demo replays offline.

## H2 — Compile the run into a playbook; replay with no model calls

This is the "learns from the job" step. It's the agent-compiler idea: a repeated agentic task becomes reviewed, deterministic code.

1. `quota/hr/compile.py` reads the exploration trajectory and writes `playbooks/bamboohr-roster.json`:
   - the login step
   - the directory URL and how the agent listed employee ids
   - the URL template of a profile tab (e.g. `/employees/employee.php?id={id}&page=…`, whatever the agent actually used)
   - per field, the locator that produced its value (label text → adjacent value, or table header → column)
2. **Review the playbook in code before it's trusted:** re-run it on the 3 sampled employees and require identical output to the agent's extraction. Fail → keep exploring.
3. `python -m quota hr sync --playbook playbooks/bamboohr-roster.json` replays it for all employees with Playwright only (zero model calls). It prints employees read, seconds elapsed, model calls (0) and writes `runs/hr/sync-<ts>.json`.
4. **Self-healing:** if a locator fails on any employee (the UI changed), mark that field `UNREAD`, fall back to the browser agent for that one page, and recompile.
5. Report both runs side by side, measured, never typed by hand:
   `Explore: N steps, M model calls, T1 s, 3 employees` / `Playbook: 0 model calls, T2 s, K employees`.

**Done when:** a sync over every employee completes with 0 model calls and the comparison line prints.

## H3 — Verify against the HR API; prove zero writes

1. `quota/hr/verify.py`:
   - fetch the same employees through the HR API (`GET /api/v1/employees`, plus the profile table endpoints the clone exposes for compensation and commission)
   - compare field by field with the playbook's output, after normalizing money, dates and whitespace
   - report `matched / total fields` and list every mismatch
2. **Zero-write proof:**
   - read `GET /__clone/ledger?since=<seq at sync start>` and `GET /__clone/diff`
   - require no write actions by the agent's session, and an empty diff
   - print `HR ledger: 0 writes by quota-onboarding (N reads)`
3. Load the verified records into Quota through `WritePath.append_fact` (`people` facts with `source = "bamboohr"` and `source_ref = profile URL`), versioned like every other fact. Reps' state and termination dates now come from HR, and the R2 and termination rules use them.
4. **Planted mess** in the clone overlay (from H0), each with an answer key:
   - one rep whose HR title says "Account Executive" but who isn't in Quota's plan list → `NO_RULE` hold, ask the plan owner
   - one rep terminated in HR whose CRM user is still active → the termination watcher fires
   - one rep whose HR commission history differs from the old payouts spreadsheet by $500 → Controller finding

**Done when:** verify prints 100% field match (or explained mismatches), 0 writes, and the three planted items are caught.

## H4 — People dashboard in Quota

A **People** tab, first after Case, built with the R7 styling.

1. **Header strip:**
   - "Onboarded from BambooHR: N employees · read Apr 1, 9:02 AM PT · 0 writes · verified 100% against the HR API"
   - a **Re-sync** button that runs the playbook and streams progress
2. **Table** (sortable, filterable by department and state; money right-aligned):
   - Name, Title, Department, Manager, State, Status
   - Base pay (from HR), Commission plan (from the learned plan, or `—`)
   - Q2 attainment, Commission paid YTD, Held, Recoverable (1290)
3. **Row click** opens a drawer:
   - the HR source (profile URL, the playbook field that read each value)
   - plan terms with their PDF quote
   - the rep's statement
   - links into the Case and Trace views
4. **Totals row:** headcount, base payroll, commission paid, commission held, and commission as a percent of revenue for the quarter.
5. **API:** `GET /api/people` and `GET /api/people/{id}`, read-only.

**Done when:** the dashboard renders from HR-synced facts, and every figure opens its trace or source.

## H5 — Quota's API contract and MCP server

1. **OpenAPI contract.** FastAPI already generates `/openapi.json`. Add `tags` and a description to every route, response models for `/api/people`, `/api/cases`, `/api/trace`, `/api/ledger/*` and `/api/scenarios`, and a `docs/API_CONTRACT.md` summary (one table: endpoint, purpose, read or write, who may call it). Writes stay limited to the existing approval, review and chat endpoints, under the `X-Principal` rules.
2. **MCP server** `quota/mcp_server.py` (Python `mcp` SDK, FastMCP; stdio and streamable HTTP). Read-only tools that call Quota's API:
   - `get_people(department?, state?)`
   - `get_rep_statement(rep_id, quarter)`
   - `get_commission_lines(status?, rep_id?)`
   - `get_case(id)`
   - `trace(kind, id, as_of?)`
   - `ledger_health()`
   - `list_scenarios()`

   One write tool, `submit_evidence(account, text, source)`, becomes an `evidence.found` event and passes the same controls.
3. **README:** `claude mcp add quota -- python -m quota.mcp_server`, and the HTTP form.
4. **Test:** call each tool through the MCP client in-process.

**Done when:** Claude Code with the MCP server attached can answer "What is Jordan Kim owed this quarter and why?" with decision IDs.

## H6 (stretch) — Write paid commissions back to HR, with proof

After Dana releases a payout file, Quota drafts one HR `commission` table row per paid line (date, amount, comment "Quota PR-2026-05 · CL-0001").
- It's posted through the HR API (the clone's employee table-row endpoint) **only after** the Payroll manager approves a `hr_writeback` approval.
- Afterward, `/__clone/diff` must show exactly those rows and nothing else; print the diff.
- Refuse the write if HR's existing commission rows for that period don't match Quota's opening lines (`HR_MISMATCH` → Controller).

## H7 (stretch) — Partner splits through Stripe Connect

**Scope rule:** employees' commissions are wages and go through payroll, never Stripe. Stripe splits are only for **external** sellers: referral partners, resellers, agencies. The README states this.

- Add one referral partner to the cast (e.g. "Northbeam Partners", 10% of first-year ACV on deals they source) with a Stripe test-mode connected account (`type: custom`, test identity data, `transfers` capability).
- When a partner-sourced deal's customer payment is collected, Quota drafts the partner fee:
  - a decision
  - an approval by the Controller
  - then a Stripe **transfer** with `transfer_group = deal id`, linked to the customer's charge (`source_transaction`) so it can't pay out before the cash arrives
- If the deal has two internal reps (split 60/40) plus the partner, show all three in one "Split" card: two payroll lines and one Stripe transfer, totals tying to the deal.
- Accounting: the partner fee is a cost to obtain the contract. Capitalize it under ASC 340-40, the same as commissions (Dr 1350 / Cr 2160 Partner fees payable, then Dr 2160 / Cr 1000 on transfer).
- Behind `--stripe`. Offline, the transfer is simulated with the same shape and a `sim_tr_` id.

## H8 (stretch) — Incentive Lab: recommend, backtest, never auto-apply

Reframe "an algorithm that sets incentives" so it survives a finance judge. Pay terms must be in a signed plan (California Labor Code 2751), and reps distrust pay that moves by formula. So Quota **recommends** plan changes, backtests them, and the VP and Controller decide. A change becomes a plan amendment that takes effect prospectively, after signature.

1. **Generator additions** per deal: gross margin %, discount %, days from opportunity to countersignature, customer health score at 90 days (a CSAT proxy), and channel CAC.
2. **Per-deal contribution:** revenue × margin − commission − partner fee − discount cost − CAC. Commission as a percent of contribution.
3. **Rule format** (code, not prompts):
   `{if: {margin_pct_gte: 70, health_gte: 8}, multiplier: 1.2}` and `{if: {discount_pct_gt: 20}, multiplier: 0.8}`, with a cap per rep per quarter.
4. **Backtest** over Q1–Q2 deals:
   - commission cost change
   - contribution change
   - reps whose pay goes down (named)
   - deals that flip tiers
5. **Screen:** "Incentive Lab" with 3 preset rules, a table of measured results, and **Propose** (creates a `plan_change_proposal` to Marcus, which needs Priya's sign-off and a signed amendment). Nothing changes until it's signed.
