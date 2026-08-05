"""
Audit trail.

Every run is persisted: the inputs, every raw signal value, the weights version
that produced it, the final score and the timestamp. A credit decision you
cannot reconstruct six months later is not a credit decision, it is a guess that
happened to be written down.

Also backs the 24 hour cache — the same table answers "have we already decided
on this domain today", so a cache hit and an audit record are the same row.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from .policy import CACHE_TTL_S, POLICY_VERSION

DB_PATH = Path(os.environ.get("MRI_DB_PATH", Path(__file__).resolve().parent.parent / "data" / "evaluations.db"))

_local = threading.local()
_init_lock = threading.Lock()
_initialised = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    domain          TEXT NOT NULL,
    input_raw       TEXT,
    declared_json   TEXT,
    policy_version  TEXT NOT NULL,
    score           REAL,
    confidence      REAL,
    decision        TEXT,
    band            TEXT,
    reserve_pct     REAL,
    payout          TEXT,
    reason_codes    TEXT,
    signals_json    TEXT,
    evidence_json   TEXT,
    result_json     TEXT,
    elapsed_ms      INTEGER,
    created_at      REAL NOT NULL,
    created_iso     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_domain ON evaluations(domain, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_eval_created ON evaluations(created_at DESC);

CREATE TABLE IF NOT EXISTS benchmark_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_version TEXT NOT NULL,
    results_json TEXT NOT NULL,
    created_at   REAL NOT NULL,
    created_iso  TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    global _initialised
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    with _init_lock:
        if not _initialised:
            conn.executescript(SCHEMA)
            conn.commit()
            _initialised = True
    _local.conn = conn
    return conn


def record(result: dict) -> int:
    """Persist one evaluation. Returns the row id, which becomes the audit id."""
    conn = _connect()
    now = time.time()
    cursor = conn.execute(
        """INSERT INTO evaluations
           (domain, input_raw, declared_json, policy_version, score, confidence,
            decision, band, reserve_pct, payout, reason_codes, signals_json,
            evidence_json, result_json, elapsed_ms, created_at, created_iso)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            result.get("domain"),
            result.get("input"),
            json.dumps(result.get("declared", {})),
            result.get("policy_version", POLICY_VERSION),
            result.get("score"),
            result.get("confidence"),
            result.get("decision"),
            result.get("band"),
            result.get("reserve_pct"),
            result.get("payout"),
            json.dumps([c["code"] for c in result.get("reason_codes", [])]),
            json.dumps(result.get("signals", [])),
            json.dumps(result.get("evidence", {})),
            json.dumps(result),
            result.get("elapsed_ms"),
            now,
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def cached(domain: str, ttl: float = CACHE_TTL_S) -> dict | None:
    """
    Most recent evaluation for this domain inside the TTL, produced by the
    current policy version. A policy change invalidates the cache: a decision
    from an older policy is history, not an answer.
    """
    conn = _connect()
    row = conn.execute(
        """SELECT result_json, created_at, created_iso, id FROM evaluations
           WHERE domain = ? AND policy_version = ? AND created_at > ?
           ORDER BY created_at DESC LIMIT 1""",
        (domain, POLICY_VERSION, time.time() - ttl),
    ).fetchone()
    if not row:
        return None
    try:
        result = json.loads(row["result_json"])
    except Exception:
        return None
    result["cached"] = True
    result["audit_id"] = row["id"]
    result["cached_at"] = row["created_iso"]
    result["cache_age_s"] = int(time.time() - row["created_at"])
    return result


def get(audit_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute(
        "SELECT result_json FROM evaluations WHERE id = ?", (audit_id,)
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["result_json"])
    except Exception:
        return None


def recent(limit: int = 25) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        """SELECT id, domain, score, confidence, decision, band, reserve_pct,
                  payout, policy_version, created_iso, elapsed_ms, reason_codes
           FROM evaluations ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        try:
            item["reason_codes"] = json.loads(item["reason_codes"] or "[]")
        except Exception:
            item["reason_codes"] = []
        out.append(item)
    return out


def stats() -> dict:
    conn = _connect()
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  COUNT(DISTINCT domain) AS domains,
                  AVG(elapsed_ms) AS avg_ms
           FROM evaluations WHERE policy_version = ?""",
        (POLICY_VERSION,),
    ).fetchone()
    return {
        "total_evaluations": row["total"] or 0,
        "distinct_domains": row["domains"] or 0,
        "avg_latency_ms": round(row["avg_ms"] or 0),
        "policy_version": POLICY_VERSION,
        "db_path": str(DB_PATH),
    }


def save_benchmark(results: dict) -> int:
    conn = _connect()
    now = time.time()
    cursor = conn.execute(
        """INSERT INTO benchmark_runs (policy_version, results_json, created_at, created_iso)
           VALUES (?,?,?,?)""",
        (POLICY_VERSION, json.dumps(results), now,
         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))),
    )
    conn.commit()
    return cursor.lastrowid


def latest_benchmark() -> dict | None:
    conn = _connect()
    row = conn.execute(
        """SELECT results_json FROM benchmark_runs
           WHERE policy_version = ? ORDER BY created_at DESC LIMIT 1""",
        (POLICY_VERSION,),
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["results_json"])
    except Exception:
        return None
