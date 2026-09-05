"""Concurrent insider-transaction fetcher.

Replaces the sequential scraper, which spent 2-5 seconds asleep between every
ticker and therefore ~29 of its ~30 minutes doing nothing. The work itself was
never the problem: ``Ticker.insider_transactions`` is a single GET to

    https://query2.finance.yahoo.com/v10/finance/quoteSummary/<symbol>

so ~500 tickers is ~500 requests, and a request takes a few hundred
milliseconds. Sequential-plus-sleep is simply the wrong shape for that.

Three changes carry the speedup:

* **Concurrency instead of naps.** A worker pool issues requests in parallel.
  ``yfinance``'s ``YfData`` is a documented thread-safe singleton — "one
  session one cookie shared by all threads" — so this is the library's own
  supported path, not a trick played on it.
* **A rate ceiling, not a per-request delay.** A token bucket caps the
  *aggregate* request rate. The old sleep throttled each worker
  independently, which means the only way to slow down was to make every
  individual ticker slower.
* **Adaptive coverage.** Most of the S&P 500 files nothing above $1M in any
  given month. Names that keep coming back empty are checked less often,
  subject to a hard ceiling so nothing is ever skipped for long.

Politeness is preserved where it actually matters. A 429 pauses *every*
worker, because a rate limit belongs to the IP rather than to the unlucky
request that tripped it; backing off one request while seven others keep
hammering is how a soft limit becomes a hard ban.
"""

from __future__ import annotations

import logging
import random
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from typing import Callable, Dict, List, Optional, Sequence

import pandas as pd

import store
from config import (
    FETCH_BURST,
    FETCH_COOLDOWN_SECONDS,
    FETCH_MAX_RETRIES,
    FETCH_RATE_PER_SECOND,
    FETCH_WORKERS,
    MAX_TICKER_SKIP_DAYS,
    MIN_TRADE_VALUE,
    STALE_TICKER_DAYS,
)

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ═══════════════════════════════════════════════════════════════════════════
# Rate limiting
# ═══════════════════════════════════════════════════════════════════════════


class TokenBucket:
    """Thread-safe token bucket capping the aggregate request rate.

    Tokens accrue continuously at ``rate`` per second up to ``burst``. A
    worker takes one before each request and blocks if the bucket is empty,
    so N workers share one budget instead of each holding their own.
    """

    def __init__(self, rate: float, burst: int) -> None:
        self._rate = max(rate, 0.1)
        self._burst = max(burst, 1)
        self._tokens = float(self._burst)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def take(self, tokens: float = 1.0) -> None:
        """Block until ``tokens`` are available, then spend them."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._burst, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now

                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return

                deficit = tokens - self._tokens
                wait = deficit / self._rate

            # Slept outside the lock so other workers can keep refilling.
            time.sleep(min(wait, 1.0))


class Cooldown:
    """A shared penalty box entered on a 429.

    Rate limits apply to the source IP, so the correct response to one worker
    being throttled is for *all* of them to stop. Each further 429 during an
    active cooldown extends it rather than restarting it, so a sustained
    throttle backs off progressively instead of oscillating.
    """

    def __init__(self, base_seconds: float) -> None:
        self._base = base_seconds
        self._until = 0.0
        self._strikes = 0
        self._lock = threading.Lock()

    def trip(self) -> float:
        """Record a rate-limit hit and return the new cooldown length."""
        with self._lock:
            self._strikes += 1
            # Capped exponential: 20s, 40s, 80s, 160s, then flat.
            penalty = self._base * min(2 ** (self._strikes - 1), 8)
            self._until = max(self._until, time.monotonic() + penalty)
            return penalty

    def clear_one(self) -> None:
        """A clean response walks one strike back off the counter."""
        with self._lock:
            if self._strikes > 0:
                self._strikes -= 1

    def wait(self) -> None:
        """Block while a cooldown is in force."""
        while True:
            with self._lock:
                remaining = self._until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 2.0))

    @property
    def strikes(self) -> int:
        with self._lock:
            return self._strikes


# ═══════════════════════════════════════════════════════════════════════════
# S&P 500 constituents
# ═══════════════════════════════════════════════════════════════════════════


def _fetch_from_wikipedia() -> Optional[List[str]]:
    """Wikipedia's constituent table. Needs a real User-Agent or it 403s."""
    try:
        req = urllib.request.Request(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8")

        tables = pd.read_html(StringIO(html))
        tickers = (
            tables[0]["Symbol"].astype(str).str.strip()
            .str.replace(".", "-", regex=False)  # BRK.B -> BRK-B, as Yahoo spells it
            .tolist()
        )
        return tickers if len(tickers) > 400 else None
    except Exception as exc:
        logger.warning("Wikipedia constituent fetch failed: %s", exc)
        return None


def _fetch_from_github() -> Optional[List[str]]:
    """The datasets/s-and-p-500-companies repo, refreshed weekly by CI."""
    try:
        req = urllib.request.Request(
            "https://raw.githubusercontent.com/datasets/"
            "s-and-p-500-companies/main/data/constituents.csv",
            headers={"User-Agent": _USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            frame = pd.read_csv(StringIO(resp.read().decode("utf-8")))

        tickers = (
            frame["Symbol"].astype(str).str.strip()
            .str.replace(".", "-", regex=False)
            .tolist()
        )
        return tickers if len(tickers) > 400 else None
    except Exception as exc:
        logger.warning("GitHub constituent fetch failed: %s", exc)
        return None


def _tickers_already_known() -> List[str]:
    """Last resort: every ticker the store has ever seen.

    Better than a hardcoded list that rots — it is by definition the universe
    this service has actually been reporting on.
    """
    try:
        state = store.get_ticker_state()
        return sorted(state.keys())
    except Exception:
        return []


def get_sp500_tickers() -> List[str]:
    """Current S&P 500 constituents, with fallbacks."""
    for name, source in (
        ("Wikipedia", _fetch_from_wikipedia),
        ("GitHub", _fetch_from_github),
    ):
        tickers = source()
        if tickers:
            logger.info("Ticker list from %s: %s symbols", name, len(tickers))
            return sorted(set(tickers))

    tickers = _tickers_already_known()
    if tickers:
        logger.warning(
            "Both constituent sources failed — falling back to the %s tickers "
            "already in the store", len(tickers),
        )
        return tickers

    raise RuntimeError(
        "Could not obtain an S&P 500 ticker list from Wikipedia or GitHub, and "
        "the trade store is empty so there is no fallback universe."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Adaptive scheduling
# ═══════════════════════════════════════════════════════════════════════════


def select_tickers(
    universe: Sequence[str],
    full: bool = False,
    state: Optional[Dict[str, Dict]] = None,
    today: Optional[date] = None,
) -> List[str]:
    """Choose which tickers this run should actually fetch.

    Every ticker is checked on a ``full`` run. Otherwise a name is skipped
    only when it has come back empty repeatedly *and* has been checked
    recently — and never for more than ``MAX_TICKER_SKIP_DAYS``, so a company
    that starts filing again surfaces within a week regardless of how quiet
    it had been.

    Unknown tickers are always fetched: a new index constituent has no
    history to judge it on.
    """
    if full:
        return list(universe)

    state = state if state is not None else store.get_ticker_state()
    today = today or datetime.now(timezone.utc).date()

    due: List[str] = []
    for ticker in universe:
        row = state.get(ticker)
        if not row or not row.get("last_checked"):
            due.append(ticker)
            continue

        try:
            checked = datetime.fromisoformat(row["last_checked"]).date()
        except (TypeError, ValueError):
            due.append(ticker)
            continue

        days_since_check = (today - checked).days
        if days_since_check >= MAX_TICKER_SKIP_DAYS:
            due.append(ticker)
            continue

        # A ticker that has filed recently stays on the daily rota.
        last_trade = row.get("last_trade_date")
        if last_trade:
            try:
                traded = datetime.strptime(last_trade, "%Y-%m-%d").date()
                if (today - traded).days <= STALE_TICKER_DAYS:
                    due.append(ticker)
                    continue
            except ValueError:
                pass

        # Quiet names get a widening interval: 1 day, then 2, 3, 4 … capped
        # by MAX_TICKER_SKIP_DAYS above.
        empties = int(row.get("consecutive_empty") or 0)
        interval = min(1 + empties // 3, MAX_TICKER_SKIP_DAYS)
        if days_since_check >= interval:
            due.append(ticker)

    return due


# ═══════════════════════════════════════════════════════════════════════════
# Fetching
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class FetchSummary:
    """What one run did — logged, and surfaced on the admin status endpoint."""

    universe: int = 0
    attempted: int = 0
    skipped: int = 0
    succeeded: int = 0
    failed: int = 0
    with_trades: int = 0
    rows_seen: int = 0
    rows_added: int = 0
    rate_limit_hits: int = 0
    seconds: float = 0.0
    failed_tickers: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {
            "universe": self.universe,
            "attempted": self.attempted,
            "skipped": self.skipped,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "tickers_with_trades": self.with_trades,
            "rows_seen": self.rows_seen,
            "rows_added": self.rows_added,
            "rate_limit_hits": self.rate_limit_hits,
            "seconds": round(self.seconds, 1),
            "failed_tickers": self.failed_tickers[:50],
        }


def _unhide_yfinance_exceptions() -> None:
    """Make yfinance raise HTTP errors instead of returning an empty frame.

    This is load-bearing, not tidiness. ``yfinance`` ships with
    ``config.debug.hide_exceptions = True``, and ``scrapers/holders.py``
    catches ``HTTPError`` under that flag and assigns an **empty DataFrame**
    to every holders field. A 429 therefore arrives here looking exactly like
    "this company has no insider transactions".

    Left alone, that is a silent-failure machine: the cooldown would never
    trip, the run would sail through its whole ticker list at full speed
    while Yahoo refused every request, and the day's report would be
    published empty rather than failing loudly.
    """
    try:
        import yfinance as yf

        yf.config.debug.hide_exceptions = False
    except Exception as exc:  # noqa: BLE001 — a config rename must not be fatal
        logger.warning(
            "Could not disable yfinance's exception hiding (%s). Rate limits "
            "may be reported as empty results.", exc,
        )


def _is_rate_limited(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "429" in text or "too many request" in text


def _is_missing_symbol(exc: BaseException) -> bool:
    """A 404 means Yahoo has no such symbol — retrying cannot help.

    Index constituent lists include names that Yahoo spells differently or
    has already delisted. Those must fail fast rather than burn four attempts
    and a rate-limit slot each, every single run.
    """
    text = str(exc).lower()
    return "404" in text or "not found" in text


def _is_retryable(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("timeout", "timed out", "connection", "temporarily",
                       "502", "503", "504", "reset")
    )


def _fetch_one(
    ticker: str,
    bucket: TokenBucket,
    cooldown: Cooldown,
    stop: threading.Event,
) -> tuple[str, Optional[pd.DataFrame], Optional[str]]:
    """Fetch one ticker's insider transactions.

    Returns ``(ticker, frame_or_None, error_or_None)``. Never raises: a single
    bad symbol must not abort a 500-ticker run.
    """
    import yfinance as yf

    for attempt in range(1, FETCH_MAX_RETRIES + 1):
        if stop.is_set():
            return ticker, None, "cancelled"

        cooldown.wait()
        bucket.take()

        try:
            frame = yf.Ticker(ticker).insider_transactions
            cooldown.clear_one()
            return ticker, frame, None

        except Exception as exc:  # noqa: BLE001 — deliberately broad, see docstring
            if _is_missing_symbol(exc):
                return ticker, None, "not found on Yahoo"

            if _is_rate_limited(exc):
                penalty = cooldown.trip()
                logger.warning(
                    "Rate limited on %s (attempt %s/%s) — pausing all workers %.0fs",
                    ticker, attempt, FETCH_MAX_RETRIES, penalty,
                )
                continue

            if _is_retryable(exc) and attempt < FETCH_MAX_RETRIES:
                wait = (2 ** attempt) + random.uniform(0, 1.5)
                logger.debug("Transient error on %s: %s — retrying in %.1fs", ticker, exc, wait)
                time.sleep(wait)
                continue

            return ticker, None, f"{type(exc).__name__}: {exc}"

    return ticker, None, "exhausted retries"


def fetch_insider_trades(
    tickers: Optional[Sequence[str]] = None,
    full: bool = False,
    progress: Optional[Callable[[int, int, str, int], None]] = None,
) -> FetchSummary:
    """Fetch insider transactions concurrently and store what is new.

    ``full`` forces every ticker in the universe to be checked, bypassing the
    adaptive schedule. Use it for the first run on a fresh volume, or after a
    long outage.

    Results are written from this thread as futures complete, so SQLite sees a
    single writer and the workers stay purely I/O-bound.
    """
    started = time.monotonic()
    store.init_db()

    universe = list(tickers) if tickers else get_sp500_tickers()
    due = select_tickers(universe, full=full)

    summary = FetchSummary(
        universe=len(universe),
        attempted=len(due),
        skipped=len(universe) - len(due),
    )

    if not due:
        logger.info("Nothing due to fetch (%s tickers all recently checked)", len(universe))
        summary.seconds = time.monotonic() - started
        return summary

    logger.info(
        "Fetching %s of %s tickers (%s skipped by the adaptive schedule) "
        "with %s workers at %.1f req/s",
        len(due), len(universe), summary.skipped, FETCH_WORKERS, FETCH_RATE_PER_SECOND,
    )

    bucket = TokenBucket(FETCH_RATE_PER_SECOND, FETCH_BURST)
    cooldown = Cooldown(FETCH_COOLDOWN_SECONDS)
    stop = threading.Event()

    _warm_session()

    completed = 0
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS, thread_name_prefix="fetch") as pool:
        futures = {
            pool.submit(_fetch_one, ticker, bucket, cooldown, stop): ticker
            for ticker in due
        }

        for future in as_completed(futures):
            ticker = futures[future]
            completed += 1

            try:
                _, frame, error = future.result()
            except Exception as exc:  # noqa: BLE001 — a worker crash is one ticker's problem
                frame, error = None, f"worker crashed: {exc}"

            if error:
                summary.failed += 1
                summary.failed_tickers.append(ticker)
                store.record_ticker_check(ticker, 0, None, error=error)
                logger.debug("Fetch failed for %s: %s", ticker, error)
            else:
                summary.succeeded += 1
                rows = _significant_rows(ticker, frame)
                summary.rows_seen += len(rows)

                if rows:
                    summary.with_trades += 1
                    added = store.upsert_trades(rows)
                    summary.rows_added += added
                    latest = max(row["trade_date"] for row in rows)
                    store.record_ticker_check(ticker, len(rows), latest)
                else:
                    store.record_ticker_check(ticker, 0, None)

            if progress:
                progress(completed, len(due), ticker, summary.rows_added)

    summary.rate_limit_hits = cooldown.strikes
    summary.seconds = time.monotonic() - started

    logger.info(
        "Fetch complete in %.1fs: %s ok, %s failed, %s new trades from %s tickers",
        summary.seconds, summary.succeeded, summary.failed,
        summary.rows_added, summary.with_trades,
    )
    return summary


def _warm_session() -> None:
    """Acquire Yahoo's cookie and crumb once, before the pool starts.

    ``YfData`` serialises crumb acquisition behind a lock. Without this, the
    first N workers all block on that lock at startup; doing it here means
    they begin already warm.

    A failure here is not fatal — the pool will simply acquire the crumb
    itself — but it is worth knowing about, because a warmup that fails with
    a 429 means the run is throttled before it has issued a single request.
    """
    _unhide_yfinance_exceptions()
    try:
        import yfinance as yf

        yf.Ticker("AAPL").insider_transactions
    except Exception as exc:  # noqa: BLE001 — a cold start is not fatal
        if _is_rate_limited(exc):
            logger.warning("Rate limited during session warmup — Yahoo is already throttling us")
        else:
            logger.debug("Session warmup did not complete: %s", exc)


def _significant_rows(ticker: str, frame: Optional[pd.DataFrame]) -> List[Dict]:
    """Normalise a response and keep only trades at or above the threshold."""
    if frame is None or frame.empty or "Value" not in frame.columns:
        return []

    rows = store.normalise_rows(ticker, frame)
    return [row for row in rows if row["abs_value"] >= MIN_TRADE_VALUE]
