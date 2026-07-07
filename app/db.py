"""
db.py — SQLite persistence layer for Pramaan.

Deliberately plain sqlite3 (no ORM) — at pilot scale (a handful of auditors,
a few hundred cases) this is simpler to read, debug, and deploy than pulling
in SQLAlchemy, and it's a one-line swap to Postgres later if/when this needs
to run on a real server instead of a single auditor's machine.
"""

import sqlite3
import json
import os
from contextlib import contextmanager
from pathlib import Path

# Vercel's deployed filesystem is read-only except /tmp. Vercel sets the
# VERCEL env var automatically on every deployment — this is not something
# you configure yourself, so this still satisfies "no environment variables
# to set up." Locally (and on a normal server), it writes next to the app
# as before.
#
# Important: /tmp on Vercel is NOT durable storage. It can be wiped on a
# cold start or when a request lands on a different function instance, so
# on Vercel this behaves like a fresh database fairly often. That's fine
# for a live demo in front of judges; it is not a real production database.
# See DEPLOY_VERCEL.md.
if os.environ.get("VERCEL"):
    DB_PATH = Path("/tmp/pramaan.db")
else:
    DB_PATH = Path(__file__).resolve().parent.parent / "pramaan.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_no TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    period TEXT,
    claimed REAL NOT NULL DEFAULT 0,
    capacity REAL NOT NULL DEFAULT 0,
    doc_sum REAL NOT NULL DEFAULT 0,
    score INTEGER NOT NULL DEFAULT 0,
    verdict TEXT NOT NULL,
    verdict_class TEXT NOT NULL,
    ran_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    invoice TEXT,
    doc_date TEXT,
    qty REAL NOT NULL DEFAULT 0,
    rate REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    severity TEXT NOT NULL,
    text TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_invoice ON documents(invoice);
CREATE INDEX IF NOT EXISTS idx_cases_name ON cases(name);
CREATE INDEX IF NOT EXISTS idx_cases_period ON cases(period);
CREATE INDEX IF NOT EXISTS idx_cases_verdict ON cases(verdict);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def next_case_no(conn) -> str:
    row = conn.execute("SELECT COUNT(*) AS n FROM cases").fetchone()
    seq = row["n"] + 1
    return f"2026-{seq:03d}"


def find_invoice_elsewhere(conn, invoice: str, exclude_case_id: int | None = None):
    """Look up whether an invoice number already exists on a *different* case."""
    if not invoice:
        return None
    query = """
        SELECT c.case_no, c.name FROM documents d
        JOIN cases c ON c.id = d.case_id
        WHERE d.invoice = ?
    """
    params = [invoice]
    if exclude_case_id is not None:
        query += " AND c.id != ?"
        params.append(exclude_case_id)
    row = conn.execute(query + " LIMIT 1", params).fetchone()
    return dict(row) if row else None


def save_case(conn, record: dict) -> None:
    conn.execute(
        """INSERT INTO cases (case_no, name, period, claimed, capacity, doc_sum, score, verdict, verdict_class, ran_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            record["case_no"], record["name"], record["period"], record["claimed"],
            record["capacity"], record["doc_sum"], record["score"], record["verdict"],
            record["verdict_class"], record["ran_at"],
        ),
    )
    case_id = conn.execute("SELECT id FROM cases WHERE case_no = ?", (record["case_no"],)).fetchone()["id"]

    for d in record["docs"]:
        conn.execute(
            "INSERT INTO documents (case_id, invoice, doc_date, qty, rate) VALUES (?, ?, ?, ?, ?)",
            (case_id, d.get("invoice", ""), d.get("date", ""), d.get("qty", 0), d.get("rate", 0)),
        )
    for f in record["flags"]:
        conn.execute(
            "INSERT INTO flags (case_id, severity, text) VALUES (?, ?, ?)",
            (case_id, f["severity"], f["text"]),
        )
    record["id"] = case_id


def list_cases(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM cases ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def get_case(conn, case_no: str) -> dict | None:
    row = conn.execute("SELECT * FROM cases WHERE case_no = ?", (case_no,)).fetchone()
    if not row:
        return None
    case = dict(row)
    case["docs"] = [dict(r) for r in conn.execute(
        "SELECT invoice, doc_date as date, qty, rate FROM documents WHERE case_id = ?", (case["id"],)
    ).fetchall()]
    case["flags"] = [dict(r) for r in conn.execute(
        "SELECT severity, text FROM flags WHERE case_id = ?", (case["id"],)
    ).fetchall()]
    return case


def clear_all(conn) -> None:
    conn.execute("DELETE FROM flags")
    conn.execute("DELETE FROM documents")
    conn.execute("DELETE FROM cases")


def search_cases(conn, query: str = "", period: str = "", verdict: str = "") -> list[dict]:
    """Search cases with optional filters."""
    sql = "SELECT * FROM cases WHERE 1=1"
    params = []
    
    if query:
        sql += " AND (name LIKE ? OR case_no LIKE ?)"
        like_query = f"%{query}%"
        params.extend([like_query, like_query])
    
    if period:
        sql += " AND period = ?"
        params.append(period)
    
    if verdict:
        sql += " AND verdict = ?"
        params.append(verdict)
    
    sql += " ORDER BY id DESC"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_case_pair(conn, case_no1: str, case_no2: str) -> tuple[dict | None, dict | None]:
    """Get two cases for comparison."""
    case1 = get_case(conn, case_no1)
    case2 = get_case(conn, case_no2)
    return (case1, case2)
