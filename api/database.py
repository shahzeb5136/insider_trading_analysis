"""Database layer for the insider report service.

Credits (the ``users`` table) live in the shared Railway Postgres so this
service, trading_agents and the report suite all spend from one wallet. If
``DATABASE_URL`` is unset the module falls back to SQLite for local
development.

Service-specific tables (``reports``, ``purchases``) stay in SQLite on the
Railway volume, which keeps the shared Postgres to just users and credits.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from api.settings import SQLITE_PATH, ensure_dirs

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    import psycopg2
    import psycopg2.extras

    _USE_POSTGRES = True
else:
    _USE_POSTGRES = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Connection helpers ───────────────────────────────────────────────────────


@contextmanager
def _pg_conn() -> Iterator[Any]:
    """Yield a psycopg2 connection wrapped in a transaction."""
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def _sqlite_conn() -> Iterator[sqlite3.Connection]:
    """Yield a sqlite3 connection to the service database."""
    ensure_dirs()
    conn = sqlite3.connect(str(SQLITE_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


# ── Initialisation ───────────────────────────────────────────────────────────


def init_db() -> None:
    """Create every table this service depends on."""
    users_ddl = """
        CREATE TABLE IF NOT EXISTS users (
            id         TEXT PRIMARY KEY,
            credits    INTEGER NOT NULL DEFAULT 0,
            email      TEXT,
            created_at TEXT NOT NULL
        );
    """

    if _USE_POSTGRES:
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(users_ddl)
    else:
        with _sqlite_conn() as conn:
            conn.execute(users_ddl)

    with _sqlite_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id             TEXT PRIMARY KEY,
                snapshot_date  TEXT NOT NULL,
                status         TEXT NOT NULL DEFAULT 'building',
                data_through   TEXT,
                trade_count    INTEGER,
                ticker_count   INTEGER,
                cluster_count  INTEGER,
                purchase_count INTEGER,
                stats          TEXT,
                report_keys    TEXT,
                error_message  TEXT,
                created_at     TEXT NOT NULL,
                completed_at   TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_reports_status
                ON reports(status, created_at);

            CREATE TABLE IF NOT EXISTS purchases (
                id            TEXT PRIMARY KEY,
                user_id       TEXT NOT NULL,
                report_id     TEXT NOT NULL,
                credits_spent INTEGER NOT NULL,
                created_at    TEXT NOT NULL,
                UNIQUE (user_id, report_id)
            );

            CREATE INDEX IF NOT EXISTS idx_purchases_user
                ON purchases(user_id, created_at);
            """
        )


# ── Users / credits (shared Postgres) ────────────────────────────────────────


def get_or_create_user(user_id: str, email: Optional[str] = None) -> Dict[str, Any]:
    """Return the user row, creating it with 0 credits if absent."""
    now = _now()

    if _USE_POSTGRES:
        with _pg_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
                if row:
                    row = dict(row)
                    if email and email != row.get("email"):
                        cur.execute(
                            "UPDATE users SET email = %s WHERE id = %s", (email, user_id)
                        )
                        row["email"] = email
                    return row
                # ON CONFLICT guards the race where two requests land together.
                cur.execute(
                    """INSERT INTO users (id, credits, email, created_at)
                       VALUES (%s, 0, %s, %s)
                       ON CONFLICT (id) DO NOTHING""",
                    (user_id, email, now),
                )
        return {"id": user_id, "credits": 0, "email": email, "created_at": now}

    with _sqlite_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row:
            row = dict(row)
            if email and email != row.get("email"):
                conn.execute("UPDATE users SET email = ? WHERE id = ?", (email, user_id))
                row["email"] = email
            return row
        conn.execute(
            """INSERT INTO users (id, credits, email, created_at)
               VALUES (?, 0, ?, ?)
               ON CONFLICT (id) DO NOTHING""",
            (user_id, email, now),
        )
    return {"id": user_id, "credits": 0, "email": email, "created_at": now}


def get_user_credits(user_id: str) -> int:
    """Credit balance, or 0 if the user has no row yet."""
    if _USE_POSTGRES:
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT credits FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
                return row[0] if row else 0

    with _sqlite_conn() as conn:
        row = conn.execute("SELECT credits FROM users WHERE id = ?", (user_id,)).fetchone()
        return row["credits"] if row else 0


def add_credits(user_id: str, amount: int) -> int:
    """Add credits and return the new balance. Creates the user if missing."""
    get_or_create_user(user_id)

    if _USE_POSTGRES:
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET credits = credits + %s WHERE id = %s RETURNING credits",
                    (amount, user_id),
                )
                return cur.fetchone()[0]

    with _sqlite_conn() as conn:
        conn.execute("UPDATE users SET credits = credits + ? WHERE id = ?", (amount, user_id))
        row = conn.execute("SELECT credits FROM users WHERE id = ?", (user_id,)).fetchone()
        return row["credits"]


def deduct_credits(user_id: str, amount: int) -> bool:
    """Atomically deduct ``amount`` credits.

    Returns False without changing anything when the balance is too low. The
    guard lives in the WHERE clause so concurrent requests cannot drive a
    balance negative.
    """
    if _USE_POSTGRES:
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE users
                       SET    credits = credits - %s
                       WHERE  id = %s AND credits >= %s
                       RETURNING credits""",
                    (amount, user_id, amount),
                )
                return cur.fetchone() is not None

    with _sqlite_conn() as conn:
        cur = conn.execute(
            "UPDATE users SET credits = credits - ? WHERE id = ? AND credits >= ?",
            (amount, user_id, amount),
        )
        return cur.rowcount > 0


def get_all_users() -> List[Dict[str, Any]]:
    """Every user and their balance, newest first."""
    sql = "SELECT id, email, credits, created_at FROM users ORDER BY created_at DESC"

    if _USE_POSTGRES:
        with _pg_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                return [dict(r) for r in cur.fetchall()]

    with _sqlite_conn() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


# ── Reports (service SQLite) ─────────────────────────────────────────────────


def _row_to_report(row: sqlite3.Row) -> Dict[str, Any]:
    report = dict(row)
    report["report_keys"] = json.loads(report["report_keys"]) if report.get("report_keys") else {}
    report["stats"] = json.loads(report["stats"]) if report.get("stats") else {}
    return report


def create_report(snapshot_date: str) -> str:
    """Insert a report in the ``building`` state and return its id."""
    report_id = str(uuid.uuid4())
    with _sqlite_conn() as conn:
        conn.execute(
            """INSERT INTO reports (id, snapshot_date, status, created_at)
               VALUES (?, ?, 'building', ?)""",
            (report_id, snapshot_date, _now()),
        )
    return report_id


def mark_report_ready(
    report_id: str,
    report_keys: Dict[str, Any],
    manifest: Dict[str, Any],
) -> None:
    """Flip a report to ``ready`` and record what it contains."""
    with _sqlite_conn() as conn:
        conn.execute(
            """UPDATE reports
               SET status = 'ready', report_keys = ?, data_through = ?,
                   trade_count = ?, ticker_count = ?, cluster_count = ?,
                   purchase_count = ?, stats = ?, completed_at = ?
               WHERE id = ?""",
            (
                json.dumps(report_keys),
                manifest.get("data_through"),
                manifest.get("trade_count"),
                manifest.get("ticker_count"),
                manifest.get("cluster_count"),
                manifest.get("purchase_count"),
                json.dumps(manifest),
                _now(),
                report_id,
            ),
        )


def mark_report_failed(report_id: str, error_message: str) -> None:
    with _sqlite_conn() as conn:
        conn.execute(
            """UPDATE reports SET status = 'failed', error_message = ?, completed_at = ?
               WHERE id = ?""",
            (error_message[:2000], _now(), report_id),
        )


def get_latest_ready_report() -> Optional[Dict[str, Any]]:
    """The newest report users can buy, or None if nothing has built yet."""
    with _sqlite_conn() as conn:
        row = conn.execute(
            "SELECT * FROM reports WHERE status = 'ready' ORDER BY completed_at DESC LIMIT 1"
        ).fetchone()
        return _row_to_report(row) if row else None


def get_report(report_id: str) -> Optional[Dict[str, Any]]:
    with _sqlite_conn() as conn:
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        return _row_to_report(row) if row else None


def list_reports(limit: int = 50) -> List[Dict[str, Any]]:
    with _sqlite_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM reports ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_report(r) for r in rows]


def has_report_for_date(snapshot_date: str) -> bool:
    """True if a report for this date is already ready or building."""
    with _sqlite_conn() as conn:
        row = conn.execute(
            """SELECT 1 FROM reports
               WHERE snapshot_date = ? AND status IN ('ready', 'building')
               LIMIT 1""",
            (snapshot_date,),
        ).fetchone()
        return row is not None


def reap_stale_building_reports() -> int:
    """Fail any report left ``building`` by a crash or redeploy.

    A build only ever runs inside a live process, so a ``building`` row that
    survives a restart can never complete and would otherwise block the
    same-day guard forever.
    """
    with _sqlite_conn() as conn:
        cur = conn.execute(
            """UPDATE reports
               SET status = 'failed',
                   error_message = 'Build interrupted by a restart',
                   completed_at = ?
               WHERE status = 'building'""",
            (_now(),),
        )
        return cur.rowcount


# ── Purchases (service SQLite) ───────────────────────────────────────────────


def get_purchase(user_id: str, report_id: str) -> Optional[Dict[str, Any]]:
    with _sqlite_conn() as conn:
        row = conn.execute(
            "SELECT * FROM purchases WHERE user_id = ? AND report_id = ?",
            (user_id, report_id),
        ).fetchone()
        return dict(row) if row else None


def list_user_purchases(user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """A user's purchases joined with their report, newest first."""
    with _sqlite_conn() as conn:
        rows = conn.execute(
            """SELECT p.id            AS purchase_id,
                      p.credits_spent AS credits_spent,
                      p.created_at    AS purchased_at,
                      r.*
               FROM   purchases p
               JOIN   reports r ON r.id = p.report_id
               WHERE  p.user_id = ?
               ORDER BY p.created_at DESC
               LIMIT ?""",
            (user_id, limit),
        ).fetchall()

        out = []
        for row in rows:
            item = dict(row)
            item["report_keys"] = json.loads(item["report_keys"]) if item.get("report_keys") else {}
            item["stats"] = json.loads(item["stats"]) if item.get("stats") else {}
            out.append(item)
        return out


class InsufficientCredits(Exception):
    """Raised when a user cannot afford a report."""


def purchase_report(user_id: str, report_id: str, cost: int) -> tuple[Dict[str, Any], bool]:
    """Charge a user for a report, exactly once.

    Returns ``(purchase, charged)``. ``charged`` is False when the user
    already owned this report, which makes the endpoint safe to retry and
    lets the frontend re-issue download links for free.

    Credits live in Postgres while purchases live in SQLite, so the two
    writes cannot share a transaction. The credit is therefore taken first
    and refunded if the purchase row fails to land.

    Raises:
        InsufficientCredits: balance below ``cost``.
    """
    existing = get_purchase(user_id, report_id)
    if existing:
        return existing, False

    if not deduct_credits(user_id, cost):
        raise InsufficientCredits(f"User {user_id} cannot afford {cost} credit(s)")

    purchase_id = str(uuid.uuid4())
    created_at = _now()
    try:
        with _sqlite_conn() as conn:
            conn.execute(
                """INSERT INTO purchases (id, user_id, report_id, credits_spent, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (purchase_id, user_id, report_id, cost, created_at),
            )
    except sqlite3.IntegrityError:
        # Two requests raced. The other one won, so give this charge back.
        add_credits(user_id, cost)
        winner = get_purchase(user_id, report_id)
        if winner:
            return winner, False
        raise
    except Exception:
        add_credits(user_id, cost)
        logger.exception("Purchase insert failed for user %s; credits refunded", user_id)
        raise

    return (
        {
            "id": purchase_id,
            "user_id": user_id,
            "report_id": report_id,
            "credits_spent": cost,
            "created_at": created_at,
        },
        True,
    )
