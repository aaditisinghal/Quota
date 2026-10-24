"""sha256 helpers and the hash chain over every WritePath insert."""

import hashlib
import json

GENESIS = "0" * 64

# How a chain row's ref_id locates the row it covers.
KEYED_TABLES = {
    "people": ("id", "version"), "deals": ("id", "version"), "contracts": ("id", "version"),
    "plans": ("id", "version"), "je_lines": ("je_id", "line_no"),
}
ID_TABLES = {
    "esign_events", "billing", "messages", "events", "decisions", "commission_lines", "precedents",
    "approvals", "journal_entries", "amortization_schedules", "payroll_files", "refusals",
}
# Status-style tables have no natural unique key; their ref_id is "<label>#<rowid>".
ROWID_TABLES = {
    "commission_line_status": "line_id", "approval_decisions": "approval_id", "je_status": "je_id",
    "payroll_status": "file_id", "periods": "period",
}
INT_KEY_COLUMNS = {"version", "line_no"}


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def row_sha(row: dict) -> str:
    return sha256_hex(json.dumps(row, sort_keys=True, separators=(",", ":")))


def link_hash(prev_hash: str, kind: str, ref_id: str, payload_sha: str, known_at: str) -> str:
    return sha256_hex(f"{prev_hash}|{kind}|{ref_id}|{payload_sha}|{known_at}")


def ref_id_for(table: str, row: dict, rowid: int) -> str:
    if table in KEYED_TABLES:
        return ":".join(str(row[col]) for col in KEYED_TABLES[table])
    if table in ID_TABLES:
        return row["id"]
    if table in ROWID_TABLES:
        return f"{row[ROWID_TABLES[table]]}#{rowid}"
    raise ValueError(f"unknown table {table!r}")


def fetch_ref(conn, kind: str, ref_id: str) -> dict | None:
    if kind in KEYED_TABLES:
        cols = KEYED_TABLES[kind]
        values = ref_id.rsplit(":", len(cols) - 1)
        if len(values) != len(cols):
            return None
        values = [int(v) if col in INT_KEY_COLUMNS else v for col, v in zip(cols, values)]
        where = " AND ".join(f"{col} = ?" for col in cols)
        found = conn.execute(f"SELECT * FROM {kind} WHERE {where}", values).fetchone()
    elif kind in ID_TABLES:
        found = conn.execute(f"SELECT * FROM {kind} WHERE id = ?", (ref_id,)).fetchone()
    elif kind in ROWID_TABLES:
        found = conn.execute(f"SELECT * FROM {kind} WHERE rowid = ?",
                             (int(ref_id.rsplit("#", 1)[1]),)).fetchone()
    else:
        return None
    return dict(found) if found is not None else None


def last_link(conn) -> tuple[str, str | None]:
    """(hash, known_at) of the newest chain row, or (GENESIS, None) for an empty chain."""
    found = conn.execute("SELECT hash, known_at FROM chain ORDER BY seq DESC LIMIT 1").fetchone()
    return (found[0], found[1]) if found else (GENESIS, None)


def chain_append(conn, kind: str, ref_id: str, row: dict, known_at: str) -> int:
    prev_hash, _ = last_link(conn)
    payload_sha = row_sha(row)
    cur = conn.execute(
        "INSERT INTO chain (kind, ref_id, payload_sha, prev_hash, hash, known_at) VALUES (?, ?, ?, ?, ?, ?)",
        (kind, ref_id, payload_sha, prev_hash, link_hash(prev_hash, kind, ref_id, payload_sha, known_at),
         known_at))
    return cur.lastrowid


def verify_chain(conn) -> tuple[bool, int | None]:
    """Recompute from genesis, re-hashing each referenced row. Returns (ok, first_bad_seq)."""
    prev_hash = GENESIS
    for link in conn.execute("SELECT * FROM chain ORDER BY seq").fetchall():
        row = fetch_ref(conn, link["kind"], link["ref_id"])
        if (link["prev_hash"] != prev_hash
                or row is None
                or row_sha(row) != link["payload_sha"]
                or link_hash(prev_hash, link["kind"], link["ref_id"], link["payload_sha"],
                             link["known_at"]) != link["hash"]):
            return False, link["seq"]
        prev_hash = link["hash"]
    return True, None


def chain_length(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM chain").fetchone()[0]
