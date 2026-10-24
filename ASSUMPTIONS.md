# Assumptions

One line per choice made where IMPLEMENTATION.md was silent or ambiguous.

- Step 1: `pytest` is not on PATH (Windows Store Python user install), so tests run as `python -m pytest -q`, which is the same runner.
- Step 1: `.claude/settings.local.json` is machine-local and is git-ignored; SPEC.md and IMPLEMENTATION.md are committed.
- Step 2: `routing.py` was written in step 2, not step 3, because `WritePath.decide_approval` needs `next_approver`.
- Step 2: each test gets its own shared-cache in-memory DB (`file:memdbN?mode=memory&cache=shared`) so tests never share state.
- Step 2: read-only in-memory connections use `PRAGMA query_only = ON`, since `mode=ro` cannot be combined with `mode=memory`; file DBs use `?mode=ro`.
- Step 2: chain `ref_id` is `id:version` for versioned facts, `je_id:line_no` for je_lines, `id` for id tables, and `<key>#<rowid>` for status tables that have no natural unique key.
- Step 2: a CLOCK_BACKWARDS refusal is logged at the last chain `known_at` (attempted time goes in `detail`) so chain time never runs backward.
- Step 2: principal `system` (simulator, capability `emit:event`) writes events; `seed` holds `*` and exists only in tests and the demo.
- Step 2: `append_fact` capability is `post:source_entry` for billing, `emit:event` for events, and `write:fact` (seed only) for other fact tables, including the initial "open" period rows and P-0000.
- Step 2: plans keep the YAML version (v3); `append_fact` accepts an explicit version only if it is above the current max.
- Step 2: a commission line's posting period is the period of `clock.now()`, so a Q1 correction can post in April after Q1 is locked.
- Step 2: line status capabilities: superseded/clawed_back need `draft:commission_line`; approved needs `approve:journal_entry` (the Controller, when she approves the line's JE); paid only via `release_payroll`.
- Step 2: `request_approval` needs `draft:approval_request`, or `draft:journal_entry` for kind journal_entry (the deferred accountant has no `draft:approval_request`); a journal_entry request writes je_status `pending_approval`.
- Step 2: the over-limit child approval is requested by the original requester (seat:commissions_analyst in the demo), matching section 5's pseudocode.
- Step 2: the first decision recorded on an approval is the binding one.
- Step 2: entry memos are written `<memo_kind>: <text>`; `post_entry` accepts a seat entry under any in-force precedent whose scope is exactly `{"kind": memo_kind}` (P-0000 for amortization).
- Step 2: `add_precedent` needs `approve:plan_exception` and the approval ids must form one satisfied chain, else INV5_NOT_APPROVED.
- Step 2: added `WritePath.add_schedule` / `cancel_schedule` (capability `draft:journal_entry`) because section 5 lists no writer for amortization_schedules.
- Step 2: `release_payroll` creates and posts the payment JE itself (source_ref = payroll file id, created_by = releaser, no decision); its INV5 authority is the payroll_file approval. Payroll file ids are `PR-<period>`, status draft -> released.
- Step 2: message ids use prefix `M-`; schedule ids use `AS-`.
- Step 2: unknown ids raise KeyError/ValueError (caller bug) rather than a refusal.
- Step 2: `trial_balance` returns only accounts with posted activity (so 6115 is absent from the demo TB, matching section 9).
- Step 2: bare dates in precedent windows cover the whole day (expires 2026-06-30 means through 2026-06-30T23:59:59Z).
