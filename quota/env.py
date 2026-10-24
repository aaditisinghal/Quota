"""Local secrets from the git-ignored `.env` at the repo root. Real environment variables always win."""

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def load_env(path=ENV_PATH) -> None:
    path = Path(path)
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value.strip() and key.strip() not in os.environ:
            os.environ[key.strip()] = value.strip()
