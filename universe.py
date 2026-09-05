"""The set of companies the service covers: the current S&P 500.

Three sources, tried in order. Wikipedia and the ``datasets`` GitHub repo are
both maintained lists; the last resort is whatever the trade store has already
seen, which is by definition the universe this service has been reporting on
and cannot rot the way a hardcoded list does.
"""

from __future__ import annotations

import logging
import urllib.request
from io import StringIO
from typing import List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


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
            .str.replace(".", "-", regex=False)  # BRK.B -> BRK-B, as EDGAR spells it
            .tolist()
        )
        return tickers if len(tickers) > 400 else None
    except Exception as exc:  # noqa: BLE001 — any failure just means "try the next source"
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("GitHub constituent fetch failed: %s", exc)
        return None


def _tickers_already_known() -> List[str]:
    """Every ticker the trade store has ever held a transaction for."""
    try:
        import store

        with store.connect() as conn:
            rows = conn.execute("SELECT DISTINCT ticker FROM trades").fetchall()
        return sorted(row[0] for row in rows if row[0])
    except Exception:  # noqa: BLE001 — a cold or missing store simply has no fallback
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
