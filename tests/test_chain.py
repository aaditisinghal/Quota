from conftest import ACCOUNTANT, balanced
from quota.ledger import db, hashing


def test_chain_verifies_and_detects_tamper(ledger, wp, conn, db_uri):
    decision_id = ledger.decision(ACCOUNTANT)
    je_id = wp.draft_entry(ACCOUNTANT, "2026-04-30", "commission_accrual: test", balanced(),
                           decision_id=decision_id)
    wp.lock_period("priya.nair", "2026-01")
    assert hashing.verify_chain(conn) == (True, None)

    target = conn.execute("SELECT seq FROM chain WHERE kind = 'je_lines' AND ref_id = ?",
                          (f"{je_id}:1",)).fetchone()[0]
    tamper = db.connect(db_uri)   # a raw writer that bypasses WritePath
    tamper.execute("UPDATE je_lines SET debit_cents = 1 WHERE je_id = ? AND line_no = 1", (je_id,))
    tamper.commit()
    tamper.close()

    assert hashing.verify_chain(conn) == (False, target)


def test_every_insert_is_chained(ledger, conn):
    ledger.decision()
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                         "AND name NOT IN ('chain', 'sqlite_sequence')")]
    rows = sum(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables)
    assert rows == hashing.chain_length(conn)


def test_genesis_and_link_format(ledger, conn):
    first = conn.execute("SELECT * FROM chain ORDER BY seq LIMIT 1").fetchone()
    assert first["prev_hash"] == "0" * 64
    assert first["hash"] == hashing.link_hash(first["prev_hash"], first["kind"], first["ref_id"],
                                              first["payload_sha"], first["known_at"])
