# Assumptions

One line per choice made where IMPLEMENTATION.md was silent or ambiguous.

- Step 1: `pytest` is not on PATH (Windows Store Python user install), so tests run as `python -m pytest -q`, which is the same runner.
- Step 1: `.claude/settings.local.json` is machine-local and is git-ignored; SPEC.md and IMPLEMENTATION.md are committed.
