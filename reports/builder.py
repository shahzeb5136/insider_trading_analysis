"""Assembles one insider briefing, end to end.

    trade store ──► analysis ──► one batched price download ──► charts ──► PDF

Ordering is the whole point. The analysis knows every ticker any chart will
need before a single figure is drawn, so the price data arrives in one request
instead of two per chart. The old pipeline discovered its ticker list while
rendering, which is why it made ~44 calls to draw ~22 charts and downloaded
the S&P 500 benchmark twenty-two times.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

import store
from config import BENCHMARK_TICKER, CHART_CACHE_DIR, LOOKBACK_DAYS
from reports import analysis, charts
from reports.pdf_report import build_pdf

logger = logging.getLogger(__name__)

# How far before the earliest charted event to start the price download. A
# little runway on the left makes the "since purchase" rebasing stable when
# the purchase date itself was a market holiday.
CHART_LEAD_DAYS = 7

# Clusters shown in full, with participants and a chart. The rest appear in
# the league table, so raising this adds pages rather than information.
SPOTLIGHT_LIMIT = 8


def build_report(
    output_path: Path,
    as_of: Optional[datetime] = None,
    skip_charts: bool = False,
) -> Dict[str, Any]:
    """Build one briefing PDF and return a manifest describing it."""
    as_of = as_of or datetime.now(timezone.utc)

    logger.info("Loading trades")
    trades = store.load_trades()
    if trades.empty:
        raise RuntimeError(
            "The trade store is empty. Run a fetch first, or bootstrap it from "
            "the shipped CSV."
        )

    logger.info("Analysing %s trades across %s tickers",
                f"{len(trades):,}", trades["ticker"].nunique())
    findings = analysis.analyse(trades, as_of=as_of, window_days=LOOKBACK_DAYS)

    prices = pd.DataFrame()
    if not skip_charts:
        prices = _load_prices(findings)
        analysis.enrich_with_performance(findings.clusters, prices, BENCHMARK_TICKER)
        _attach_charts(findings, prices)
    else:
        logger.info("Skipping charts and price download (--skip-charts)")

    build_pdf(findings, output_path, spotlight_limit=SPOTLIGHT_LIMIT)

    charts.prune_cache()

    return {
        "snapshot_date": findings.as_of,
        "data_through": findings.history_end,
        "trade_count": findings.total_trades,
        "ticker_count": findings.total_tickers,
        "window_days": findings.window_days,
        "purchase_count": int(len(findings.purchases)),
        "purchase_value": float(findings.purchase_value),
        "sale_value": float(findings.sale_value),
        "cluster_count": len(findings.clusters),
        "new_cluster_count": len(findings.recent_clusters),
        "filename": output_path.name,
        "path": str(output_path),
        "bytes": output_path.stat().st_size,
    }


def _load_prices(findings: analysis.Findings) -> pd.DataFrame:
    """One batched download covering every series the report needs."""
    tickers = findings.chart_tickers()
    if not tickers:
        logger.info("No tickers need price data")
        return pd.DataFrame()

    earliest = _earliest_date(findings)
    start = (
        datetime.strptime(earliest, "%Y-%m-%d") - timedelta(days=CHART_LEAD_DAYS)
    ).strftime("%Y-%m-%d")

    logger.info("Fetching %s price series from %s", len(tickers), start)
    return charts.fetch_price_history(tickers, start=start)


def _earliest_date(findings: analysis.Findings) -> str:
    """The oldest date any chart or performance figure reaches back to."""
    candidates = [c.date for c in findings.clusters]
    if not findings.purchases.empty:
        candidates.append(str(findings.purchases["trade_date"].min()))
    if not candidates:
        candidates.append(findings.window_start)
    return min(candidates)


def _attach_charts(findings: analysis.Findings, prices: pd.DataFrame) -> None:
    """Render a chart per spotlighted cluster and attach the paths."""
    if prices.empty:
        return

    spotlight = findings.by_conviction(SPOTLIGHT_LIMIT)
    requests = [
        (
            cluster.ticker,
            cluster.date,
            f"{cluster.ticker} — total return since the cluster of {cluster.date_long}",
        )
        for cluster in spotlight
    ]

    rendered = charts.build_charts(requests, prices, cache_dir=CHART_CACHE_DIR)
    for cluster in spotlight:
        cluster.chart_path = rendered.get((cluster.ticker, cluster.date))

    logger.info("Attached %s of %s spotlight charts",
                sum(1 for c in spotlight if c.chart_path), len(spotlight))
