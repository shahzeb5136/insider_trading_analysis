"""The insider trade store.

SQLite on the Railway volume, replacing the CSV the original scripts appended
to. Three reasons the file format changed:

* **Dedup needs a key.** The daily SEC index scan deliberately overlaps a
  week so late and amended filings are caught, which means most of what a
  run sees is already stored. The CSV version deduped with
  ``drop_duplicates()`` over the entire frame — O(n) on every checkpoint and
  silently wrong the moment one field is reformatted upstream. Here each row
  carries a deterministic key and re-inserting it is a no-op.
* **Reads are predicated.** The report wants "purchases in the last 10 days"
  and "clusters since 2025", not the whole file. An index answers those; a
  CSV parse does not.
* **Interrupted writes must not truncate.** A ``to_csv`` killed mid-flush
  leaves a half file that still parses. A transaction does not.

The store is deliberately dumb: it holds rows and answers questions about
them. Every judgement about what a row *means* lives in ``reports/analysis``.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

import pandas as pd

from config import TRADES_DB_PATH, ensure_dirs

logger = logging.getLogger(__name__)

TRADE_COLUMNS = [
    "trade_key", "ticker", "insider", "position", "trade_date", "trade_type",
    "text", "transaction_desc", "shares", "price", "value", "abs_value",
    "ownership", "trans_code", "is_plan", "source", "first_seen",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Yield a connection to the trade store, committing on clean exit."""
    ensure_dirs()
    conn = sqlite3.connect(str(path or TRADES_DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL lets the report read while a fetch is still writing.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db(path: Path | None = None) -> None:
    """Create the schema. Safe to call on every boot."""
    with connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trades (
                trade_key   TEXT PRIMARY KEY,
                ticker      TEXT NOT NULL,
                insider     TEXT,
                position    TEXT,
                trade_date  TEXT NOT NULL,
                trade_type  TEXT NOT NULL,
                text        TEXT,
                transaction_desc TEXT,
                shares      REAL,
                price       REAL,
                value       REAL,
                abs_value   REAL,
                ownership   TEXT,
                -- SEC transaction code (P, S, M, A, F, G …). The filer's own
                -- classification, which is why the report never has to infer
                -- "is this a purchase?" from prose.
                trans_code  TEXT,
                -- Whether the trade was made under a pre-arranged Rule 10b5-1
                -- plan. This is the line between a scheduled disposal and a
                -- decision, and no other field carries it.
                is_plan     INTEGER NOT NULL DEFAULT 0,
                source      TEXT NOT NULL DEFAULT 'sec',
                first_seen  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_trades_date
                ON trades(trade_date DESC);
            CREATE INDEX IF NOT EXISTS idx_trades_ticker_date
                ON trades(ticker, trade_date DESC);
            CREATE INDEX IF NOT EXISTS idx_trades_type_date
                ON trades(trade_type, trade_date DESC);
            CREATE INDEX IF NOT EXISTS idx_trades_absvalue
                ON trades(abs_value DESC);

            -- Every SEC filing already downloaded and parsed.
            --
            -- A daily run re-scans the last week of indexes so that late and
            -- amended filings are picked up. Without this table it would also
            -- re-download the ~550 filings it already has, every single day.
            -- With it, only genuinely new documents are fetched, so a routine
            -- run costs a handful of index requests plus whatever was filed
            -- since yesterday.
            CREATE TABLE IF NOT EXISTS seen_filings (
                path        TEXT PRIMARY KEY,
                fetched_at  TEXT NOT NULL,
                rows_found  INTEGER NOT NULL DEFAULT 0
            );

            -- Which daily filing indexes have been fully processed.
            --
            -- SEC publishes the bulk quarterly datasets a quarter in arrears,
            -- so on any given day there is a gap of up to five months between
            -- the newest bulk file and the week the daily scan covers. This
            -- table is how that gap gets closed a chunk at a time: each build
            -- works through the oldest unprocessed days, and a run that is
            -- interrupted resumes exactly where it stopped instead of
            -- starting the whole backfill again.
            CREATE TABLE IF NOT EXISTS index_days (
                day           TEXT PRIMARY KEY,
                processed_at  TEXT NOT NULL,
                filings_found INTEGER NOT NULL DEFAULT 0,
                rows_added    INTEGER NOT NULL DEFAULT 0
            );

            -- Small facts about the store itself. Today: which quarterly bulk
            -- datasets actually loaded, so the gap fill starts from what the
            -- store holds rather than from what SEC has published.
            CREATE TABLE IF NOT EXISTS meta (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            """
        )

        _migrate(conn)


def set_meta(key: str, value: str, path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, _now()),
        )


def get_meta(key: str, path: Path | None = None) -> Optional[str]:
    with connect(path) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None


# Columns added after the first version shipped. ``CREATE TABLE IF NOT
# EXISTS`` does nothing to a table that already exists, so without this a
# redeploy onto a warm Railway volume would keep the old four-column-shorter
# ``trades`` table and every insert would fail on the unknown columns.
_TRADE_COLUMN_MIGRATIONS: Tuple[Tuple[str, str], ...] = (
    ("price", "REAL"),
    ("trans_code", "TEXT"),
    ("is_plan", "INTEGER NOT NULL DEFAULT 0"),
    ("source", "TEXT NOT NULL DEFAULT 'sec'"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to the current schema.

    Deliberately additive only: columns are added, never dropped or retyped,
    so rolling back to a previous release cannot lose data. SQLite's
    ``ALTER TABLE ADD COLUMN`` is a metadata-only operation, so this stays
    fast however large the table is. (A ``ticker_state`` table from the
    Yahoo-era schema may still exist on old volumes; it is simply left alone.)
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}

    for column, definition in _TRADE_COLUMN_MIGRATIONS:
        if column in existing:
            continue
        conn.execute(f"ALTER TABLE trades ADD COLUMN {column} {definition}")
        logger.info("Migrated trades: added column %s", column)


# ── Identity ─────────────────────────────────────────────────────────────────

def trade_key(
    ticker: str,
    insider: str,
    trade_date: str,
    shares: Any,
    value: Any,
    text: str,
    accession: str = "",
    ownership: str = "",
    occurrence: int = 0,
) -> str:
    """Deterministic identity for one transaction line of one filing.

    EDGAR exposes no per-transaction ID, so identity is built from the filing
    (its accession number), the line's content, and the line's occurrence
    index among identical lines in the same filing. The last part is what
    keeps two identical $2M lots in one Form 4 from collapsing into one row,
    without depending on the order the bulk TSV and the XML list them in.

    Shares and value are rounded before hashing: they arrive as floats and a
    change in the last decimal place would otherwise duplicate a row that is
    plainly the same filing.
    """

    def _num(x: Any) -> str:
        try:
            return f"{float(x):.2f}"
        except (TypeError, ValueError):
            return ""

    raw = "\x1f".join(
        (
            (ticker or "").strip().upper(),
            (insider or "").strip().upper(),
            (trade_date or "").strip(),
            _num(shares),
            _num(value),
            (text or "").strip()[:160].upper(),
            (accession or "").strip(),
            (ownership or "").strip().upper()[:1],
            str(int(occurrence or 0)),
        )
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ── Writes ───────────────────────────────────────────────────────────────────

def upsert_trades(rows: Sequence[Dict[str, Any]], path: Path | None = None) -> int:
    """Insert rows, ignoring ones already stored. Returns the number added.

    ``INSERT OR IGNORE`` against the primary key is the whole dedup strategy —
    re-fetching a filing whose lines have not changed writes nothing and
    costs one index probe per row.
    """
    if not rows:
        return 0

    prepared = [
        {"price": None, "trans_code": None, "is_plan": 0, "source": "sec", **row}
        for row in rows
    ]

    with connect(path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        conn.executemany(
            """INSERT OR IGNORE INTO trades
               (trade_key, ticker, insider, position, trade_date, trade_type,
                text, transaction_desc, shares, price, value, abs_value,
                ownership, trans_code, is_plan, source, first_seen)
               VALUES
               (:trade_key, :ticker, :insider, :position, :trade_date, :trade_type,
                :text, :transaction_desc, :shares, :price, :value, :abs_value,
                :ownership, :trans_code, :is_plan, :source, :first_seen)""",
            prepared,
        )
        after = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

    return after - before


# ── SEC filing cache ─────────────────────────────────────────────────────────

def filter_unseen_filings(paths: Sequence[str], path: Path | None = None) -> List[str]:
    """Drop filing paths already downloaded. Preserves order."""
    if not paths:
        return []

    with connect(path) as conn:
        seen = {row[0] for row in conn.execute("SELECT path FROM seen_filings").fetchall()}
    return [p for p in paths if p not in seen]


def record_filings_seen(
    entries: Sequence[Tuple[str, int]], path: Path | None = None
) -> None:
    """Mark filings as downloaded. ``entries`` is ``(path, rows_found)``."""
    if not entries:
        return

    now = _now()
    with connect(path) as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO seen_filings (path, fetched_at, rows_found)
               VALUES (?, ?, ?)""",
            [(p, now, n) for p, n in entries],
        )


def prune_seen_filings(keep_days: int = 120, path: Path | None = None) -> int:
    """Forget filings older than ``keep_days``. Returns the number removed.

    The cache only needs to cover the daily-index scan window. Keeping it
    unbounded would grow it by ~150 rows a day forever for no benefit.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    with connect(path) as conn:
        cur = conn.execute("DELETE FROM seen_filings WHERE fetched_at < ?", (cutoff,))
        return cur.rowcount


def processed_index_days(path: Path | None = None) -> Set[str]:
    """Every daily index already worked through, as ``YYYY-MM-DD``."""
    with connect(path) as conn:
        return {row[0] for row in conn.execute("SELECT day FROM index_days").fetchall()}


def record_index_day(
    day: str, filings_found: int, rows_added: int, path: Path | None = None
) -> None:
    """Mark one daily index as processed."""
    with connect(path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO index_days
                   (day, processed_at, filings_found, rows_added)
               VALUES (?, ?, ?, ?)""",
            (day, _now(), filings_found, rows_added),
        )


def index_coverage(path: Path | None = None) -> Dict[str, Any]:
    """How much of the daily-index history has been processed."""
    with connect(path) as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS days, MIN(day) AS first_day, MAX(day) AS last_day
               FROM index_days"""
        ).fetchone()
    return dict(row)


# ── Reads ────────────────────────────────────────────────────────────────────

def load_trades(
    since: Optional[str] = None,
    trade_types: Optional[Iterable[str]] = None,
    min_abs_value: Optional[float] = None,
    path: Path | None = None,
) -> pd.DataFrame:
    """Load trades as a DataFrame, filtered in SQL rather than in pandas.

    Returns an empty frame with the right columns when nothing matches, so
    callers never have to special-case the cold-start shape.
    """
    clauses: List[str] = []
    params: List[Any] = []

    if since:
        clauses.append("trade_date >= ?")
        params.append(since)
    if trade_types:
        types = list(trade_types)
        clauses.append(f"trade_type IN ({','.join('?' * len(types))})")
        params.extend(types)
    if min_abs_value is not None:
        clauses.append("abs_value >= ?")
        params.append(min_abs_value)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM trades {where} ORDER BY trade_date DESC, abs_value DESC"

    with connect(path) as conn:
        frame = pd.read_sql_query(sql, conn, params=params)

    if frame.empty:
        frame = pd.DataFrame(columns=TRADE_COLUMNS)

    frame["date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["is_plan"] = pd.to_numeric(frame["is_plan"], errors="coerce").fillna(0).astype(bool)
    return frame


def stats(path: Path | None = None) -> Dict[str, Any]:
    """Summary of what the store holds — drives ``/api/admin/status``."""
    with connect(path) as conn:
        row = conn.execute(
            """SELECT COUNT(*)                AS trades,
                      COUNT(DISTINCT ticker)  AS tickers,
                      MIN(trade_date)         AS first_trade,
                      MAX(trade_date)         AS last_trade,
                      SUM(is_plan)            AS plan_trades
               FROM trades"""
        ).fetchone()
        filings = conn.execute("SELECT COUNT(*) FROM seen_filings").fetchone()[0]

    out = dict(row)
    out["plan_trades"] = int(out.get("plan_trades") or 0)
    out["filings_cached"] = filings
    return out


def is_empty(path: Path | None = None) -> bool:
    """True when the store holds no trades at all."""
    with connect(path) as conn:
        return conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
