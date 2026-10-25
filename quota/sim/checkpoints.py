"""Named checkpoints in the Acme story (R4).

A checkpoint is a moment in the scripted demo. `snapshot` copies the SQLite ledger there; `restore` rebuilds the
world to that moment (the story is deterministic) and checks the rebuilt hash chain against the snapshot's, so a
restored world is the snapshotted ledger plus the clock, queue and people that a file alone cannot carry.
"""

import sqlite3
from pathlib import Path

from quota.ledger import hashing
from quota.sim import demo_acme, seed_acme

CHECKPOINTS = {
    "cp_dispute": "2026-04-27T18:30:00Z",       # after "escalate"
    "cp_routed": "2026-04-28T15:00:00Z",        # A-0002 routed to Priya (Jordan's 15:01 ask is still to come)
    "cp_april_close": "2026-05-01T00:00:00Z",
    "cp_may_close": "2026-05-31T23:59:00Z",
    "cp_end": demo_acme.REPORT_AT,
}
DEFAULT_DIR = Path(".checkpoints")


class CheckpointMismatch(RuntimeError):
    pass


def snapshot(world, name, directory=DEFAULT_DIR) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.sqlite"
    path.unlink(missing_ok=True)
    target = sqlite3.connect(path)
    try:
        world.conn.backup(target)
    finally:
        target.close()
    return path


def snapshot_head(name, directory=DEFAULT_DIR) -> str | None:
    path = Path(directory) / f"{name}.sqlite"
    if not path.exists():
        return None
    conn = sqlite3.connect(path)
    try:
        return hashing.last_link(conn)[0]
    finally:
        conn.close()


def ensure_snapshots(directory=DEFAULT_DIR) -> dict[str, Path]:
    """Run the scripted demo once, snapshotting each checkpoint, unless they are all on disk already."""
    paths = {name: Path(directory) / f"{name}.sqlite" for name in CHECKPOINTS}
    if not all(p.exists() for p in paths.values()):
        demo = demo_acme.AcmeDemo(out=lambda line: None)
        try:
            demo.run(checkpoint_dir=directory)
        finally:
            demo.close()
    return paths


def restore(name, edit=None, seed_kwargs=None, directory=DEFAULT_DIR, verify=True) -> demo_acme.AcmeDemo:
    """The scripted world at a checkpoint ("start" is just after seeding). `edit` changes the script and
    `seed_kwargs` the seed; a scenario's edits only touch what happens after its checkpoint, which the snapshot
    check proves for edits (a changed seed is a different world and is not checked)."""
    demo = demo_acme.AcmeDemo(out=lambda line: None, edit=edit)
    seed_acme.seed(demo.world.wp, demo.world.clock, demo.world.emit, **(seed_kwargs or {}))
    if name != "start":
        demo.world.run_until(CHECKPOINTS[name])
        expected = snapshot_head(name, directory) if verify and not seed_kwargs else None
        if expected is not None and hashing.last_link(demo.world.conn)[0] != expected:
            demo.close()
            raise CheckpointMismatch(f"rebuilt {name} does not match its snapshot")
    return demo
