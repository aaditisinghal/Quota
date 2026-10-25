# Quota's changes to the BambooHR clone

The BambooHR clone (`runfrist/bamboohr-clone`) is a separate project that ships BambooHR's own markup, CSS and
assets, so it is not copied into this repository. This folder holds only Quota's changes to it (CUA_TASKS C1):

- `quota-clone.patch`: every change, as a git patch against the clone as received.
- `files/`: the files Quota added, for reading on GitHub.

| File | What it does |
| --- | --- |
| `app/backend/backend-generation-v1/src/seed/quota-roster.mjs` | With `QUOTA_ROSTER` set, Quota's cast replaces the generated Sales department; the generated Sales people move to Customer Success. Deterministic. |
| `app/backend/backend-generation-v1/tests/quota-roster.test.mjs` | Same roster, same world; the roster lands in Sales, Finance, Sales Operations and Partners. |
| `scripts/*.mjs` | `bun run setup`, `build`, `start`, `test`, `reset` (missing from the copy received; they follow the Dockerfile). |
| `start-clone.cmd` | Starts the clone on Windows with Quota's roster on http://127.0.0.1:8080. |
| `app/frontend/react-clone/postcss.config.mjs` | Stops Vite from picking up a PostCSS config from a parent folder. |

The patch also hooks `applyQuotaRoster` into `generate.mjs` and makes the test harness's temp and server paths
portable.

## Apply

```sh
cd bamboohr-clone
git apply ../Quota/integrations/bamboohr-clone/quota-clone.patch
python -m quota export-roster --world acme+fernhill --seed 7 --out out/hr_roster.json   # in Quota
QUOTA_ROSTER=../Quota/out/hr_roster.json CLONE_ADMIN_TOKEN=dev PORT=8080 bun run start
```

Status: the cast seeds deterministically and shows in the People directory. The clone's own suite still has 6
failures on Windows (its `api.test.mjs` keeps its database under `/tmp`).
