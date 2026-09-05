"""Configuration for the insider-trading service.

Everything tunable lives here: where data is stored, which tickers are
scanned, what counts as a significant trade, and how hard the fetchers are
allowed to push SEC EDGAR and Yahoo Finance.

Paths resolve from ``DATA_DIR`` so the same code runs against a repo-relative
``./data`` locally and the Railway volume at ``/data`` in production.
"""

from __future__ import annotations

import os
from pathlib import Path

# ── Filesystem ───────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent

# Railway volume mount point. Everything that must survive a redeploy lives
# here. Locally this falls back to ./data, which is gitignored.
DATA_DIR = Path(os.getenv("DATA_DIR", str(REPO_ROOT / "data")))

# The trade store. SQLite rather than the old CSV: the fetcher writes from
# several threads, the report reads with date-range predicates, and an
# interrupted run must not truncate the file.
TRADES_DB_PATH = Path(os.getenv("TRADES_DB_PATH", str(DATA_DIR / "insider_trades.db")))

# Cached daily closes backing the report's performance charts. Kept beside the
# trades so a warm volume never re-downloads a price series it already has.
PRICE_CACHE_PATH = Path(os.getenv("PRICE_CACHE_PATH", str(DATA_DIR / "price_cache.db")))

# Rendered chart PNGs, keyed by ticker + window. Survives across builds, so a
# cluster charted last week is not re-rendered today.
CHART_CACHE_DIR = Path(os.getenv("CHART_CACHE_DIR", str(DATA_DIR / "charts")))


# ── What counts as a trade worth reporting ───────────────────────────────────

# Minimum absolute dollar value for a transaction to enter the store at all.
MIN_TRADE_VALUE = int(os.getenv("MIN_TRADE_VALUE", "1000000"))

# Window the daily report covers.
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "10"))

# ── Cluster analysis ─────────────────────────────────────────────────────────
# A "cluster" is several insiders at the same company buying on the same day —
# the single strongest signal in the dataset, because it is hard to explain as
# a scheduled or personal-liquidity trade.

# Distinct insiders who must buy the same ticker on the same day.
MIN_CLUSTER_INSIDERS = int(os.getenv("MIN_CLUSTER_INSIDERS", "2"))

# Minimum value of each individual purchase inside a cluster.
MIN_CLUSTER_VALUE_PER_INSIDER = int(
    os.getenv("MIN_CLUSTER_VALUE_PER_INSIDER", "1000000")
)

# How far back the cluster section looks. The full history is kept in the
# store, but a briefing that opens with a 2024 cluster buries the ones that
# still matter. 0 means "no limit".
CLUSTER_LOOKBACK_DAYS = int(os.getenv("CLUSTER_LOOKBACK_DAYS", "540"))

# Words in the transaction text that mark a genuine open-market purchase.
# Deliberately narrow: "acquisition" also matches option acquisitions, which
# are compensation rather than conviction.
PURCHASE_KEYWORDS = ("purchase",)


# ── SEC EDGAR ────────────────────────────────────────────────────────────────
# The authoritative source. Form 4s reach EDGAR the day they are filed, carry
# the full history rather than a truncated window, and — most usefully — state
# the SEC transaction code, so an open-market purchase is identified by the
# filer's own "P" rather than by grepping prose for the word "purchase".
#
# The service used to read this data from Yahoo Finance. Yahoo caps insider
# history at 150 rows per ticker, runs a median ~18 days behind, and was
# measured missing $55M of Cascade Investment buying in RSG that EDGAR had
# published on the filing date. For a daily briefing that is disqualifying.

# SEC requires a declared User-Agent carrying a real contact address; requests
# without one are refused. Set SEC_USER_AGENT in the environment to change it.
SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT",
    "NexGen Solutions insider-research y.shahzeb@gmail.com",
)

# SEC's published limit is 10 requests/second. 8 leaves headroom for the
# retry path without ever approaching it.
SEC_RATE_PER_SECOND = float(os.getenv("SEC_RATE_PER_SECOND", "8.0"))
SEC_WORKERS = int(os.getenv("SEC_WORKERS", "8"))
SEC_MAX_RETRIES = int(os.getenv("SEC_MAX_RETRIES", "3"))

# How many days back a routine incremental run scans the daily indexes. Form 4
# is due within two business days of the transaction, so a week of overlap
# comfortably covers weekends, holidays and late filings. Re-reading a day
# costs one index request and dedupes to nothing.
SEC_LOOKBACK_DAYS = int(os.getenv("SEC_LOOKBACK_DAYS", "7"))

# Quarters of bulk history to pull when the store is cold. Each quarter is a
# single ~10MB ZIP that parses in about a second, so this is cheap.
SEC_BACKFILL_QUARTERS = int(os.getenv("SEC_BACKFILL_QUARTERS", "10"))

# Trading days of the bulk-to-daily gap to close per build.
#
# SEC publishes each quarterly dataset months after the quarter ends, so a
# store built from bulk plus a week of dailies has a hole of up to five
# months in the middle. Closing it costs roughly 15 seconds a day, so doing
# it all at once would block a cold start for the better part of an hour.
# Instead each build takes a bite, oldest first, and the history is complete
# within a few days while the service is useful from the first one.
SEC_GAP_DAYS_PER_RUN = int(os.getenv("SEC_GAP_DAYS_PER_RUN", "45"))


# ── Fetching (price data) ────────────────────────────────────────────────────
# Yahoo Finance is still used for the report's performance charts — one
# batched request per build, which is negligible against the shared budget.
# The settings below also drive the legacy Yahoo trade fetcher, kept as a
# fallback path.

# Worker threads issuing Yahoo requests.
FETCH_WORKERS = int(os.getenv("FETCH_WORKERS", "8"))

# Sustained ceiling on outbound requests. Yahoo's unofficial limit is widely
# reported at ~360/hour for authenticated-feeling traffic and far higher for
# quoteSummary; 4/s ≈ 240/min is well inside what a single IP sustains, and
# it is the number to lower first if 429s appear.
FETCH_RATE_PER_SECOND = float(os.getenv("FETCH_RATE_PER_SECOND", "4.0"))

# Burst allowance for the token bucket — lets a run start at full speed
# instead of ramping, without raising the sustained rate.
FETCH_BURST = int(os.getenv("FETCH_BURST", "8"))

# Retries per ticker on a 429 or a transport error.
FETCH_MAX_RETRIES = int(os.getenv("FETCH_MAX_RETRIES", "4"))

# On a 429 every worker backs off together for this long, because a rate
# limit is a property of the IP and not of the ticket that happened to hit
# it. Retrying just the one request would keep the other seven workers
# hammering straight through the penalty box.
FETCH_COOLDOWN_SECONDS = float(os.getenv("FETCH_COOLDOWN_SECONDS", "20.0"))

# A ticker that has not filed anything in this long is checked less often.
# Most of the S&P 500 files nothing above $1M in any given week, and those
# names are the bulk of the request budget.
STALE_TICKER_DAYS = int(os.getenv("STALE_TICKER_DAYS", "45"))

# ...but never skip a ticker for longer than this, so a name that starts
# filing again is picked up within days rather than whenever it next trends.
MAX_TICKER_SKIP_DAYS = int(os.getenv("MAX_TICKER_SKIP_DAYS", "7"))


# ── Charts ───────────────────────────────────────────────────────────────────

# Benchmark every performance chart is drawn against.
BENCHMARK_TICKER = "^GSPC"
BENCHMARK_LABEL = "S&P 500"

# Processes rendering matplotlib figures in parallel. Rendering is CPU-bound
# and matplotlib is not thread-safe, so this is processes, not threads.
CHART_WORKERS = int(os.getenv("CHART_WORKERS", "4"))


def ensure_dirs() -> None:
    """Create every directory the service writes to."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CHART_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    TRADES_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    PRICE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
