"""Performance charts for the insider report.

The old implementation called ``yf.download()`` twice per chart — once for the
ticker, once for the S&P 500 — inside the loop that emitted them. With 22
clusters that is 44 downloads, 22 of which fetch the identical benchmark
series. Charts were also re-rendered from scratch every run, including the
ones whose underlying window had not moved since yesterday.

This version inverts the order of operations:

    collect every ticker any chart will need   (analysis knows this up front)
        └─► ONE batched download for all of them, benchmark included
                └─► render only the figures not already cached on disk
                        └─► in parallel, across processes

The download goes from ~44 requests to 1, and a warm cache renders nothing at
all. Styling matches the PDF house style — light background, navy accents,
green/red directional colour — rather than the dark theme the standalone
script used, which looked wrong embedded in a white page.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import matplotlib

matplotlib.use("Agg")  # no display on a Railway container

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

from config import (
    BENCHMARK_LABEL,
    BENCHMARK_TICKER,
    CHART_CACHE_DIR,
    CHART_WORKERS,
)

logger = logging.getLogger(__name__)

# ── House palette, shared with the PDF ───────────────────────────────────────
INK = "#1a1a2e"
NAVY = "#0f3460"
MUTED = "#666666"
GRID = "#e4e7ec"
UP = "#2e7d32"
DOWN = "#c62828"
BENCH = "#94a3b8"


# ═══════════════════════════════════════════════════════════════════════════
# Price data
# ═══════════════════════════════════════════════════════════════════════════


def fetch_price_history(
    tickers: Sequence[str],
    start: str,
    end: Optional[str] = None,
) -> pd.DataFrame:
    """Download adjusted closes for every ticker at once.

    Returns a DataFrame indexed by date with one column per ticker, benchmark
    included. Tickers Yahoo cannot serve are simply absent from the result —
    a chart for a delisted name is not worth failing a report over.

    One request covers the whole set: ``yf.download`` batches symbols server
    side, which is the single biggest saving in the chart pipeline.
    """
    import yfinance as yf

    wanted = sorted({t.strip().upper() for t in tickers if t} | {BENCHMARK_TICKER})
    if not wanted:
        return pd.DataFrame()

    end = end or (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    logger.info("Downloading %s price series from %s in one call", len(wanted), start)

    try:
        raw = yf.download(
            wanted,
            start=start,
            end=end,
            progress=False,
            auto_adjust=True,
            group_by="column",
            threads=True,
        )
    except Exception:
        logger.exception("Batched price download failed")
        return pd.DataFrame()

    if raw is None or raw.empty:
        logger.warning("Batched price download returned nothing")
        return pd.DataFrame()

    # yfinance returns a MultiIndex (field, ticker) for multiple symbols and a
    # flat frame for one. Normalise both to a plain ticker-per-column frame.
    if isinstance(raw.columns, pd.MultiIndex):
        if "Close" not in raw.columns.get_level_values(0):
            logger.warning("Price download has no Close level")
            return pd.DataFrame()
        closes = raw["Close"]
    else:
        if "Close" not in raw.columns:
            return pd.DataFrame()
        closes = raw[["Close"]].rename(columns={"Close": wanted[0]})

    closes = closes.dropna(axis=1, how="all")
    missing = set(wanted) - set(closes.columns)
    if missing:
        logger.info("No price data for %s", ", ".join(sorted(missing)))

    return closes


# ═══════════════════════════════════════════════════════════════════════════
# Rendering
# ═══════════════════════════════════════════════════════════════════════════


def _cache_key(ticker: str, start: str, through: str) -> str:
    """Identity of a rendered figure.

    ``through`` is the last date in the price data, so a chart is reused only
    while both its window and its data are unchanged. A new trading day
    changes ``through`` and the figure is redrawn.
    """
    safe = ticker.replace("^", "_").replace("/", "-")
    return f"{safe}__{start}__{through}.png"


def _render_one(job: Dict) -> Tuple[str, Optional[str]]:
    """Render one performance chart. Runs in a worker process.

    Takes plain data rather than objects because everything crossing a
    process boundary has to pickle, and a matplotlib figure does not.
    """
    ticker = job["ticker"]
    fig = None
    try:
        series = pd.Series(job["values"], index=pd.to_datetime(job["dates"]))
        bench = (
            pd.Series(job["bench_values"], index=pd.to_datetime(job["bench_dates"]))
            if job.get("bench_values")
            else None
        )

        if len(series) < 2:
            return ticker, None

        # Rebase both to 0% at the purchase date so the comparison is like
        # for like regardless of share price.
        rebased = (series / series.iloc[0] - 1.0) * 100.0
        final = float(rebased.iloc[-1])
        colour = UP if final >= 0 else DOWN

        fig, ax = plt.subplots(figsize=(6.6, 2.4))
        fig.patch.set_facecolor("white")
        ax.set_facecolor("white")

        ax.plot(rebased.index, rebased.values, color=colour, linewidth=1.7,
                label=f"{ticker}  {final:+.1f}%", zorder=3)
        ax.fill_between(rebased.index, rebased.values, 0, color=colour, alpha=0.09, zorder=2)

        if bench is not None and len(bench) >= 2:
            bench_rebased = (bench / bench.iloc[0] - 1.0) * 100.0
            bench_final = float(bench_rebased.iloc[-1])
            ax.plot(bench_rebased.index, bench_rebased.values, color=BENCH,
                    linewidth=1.2, linestyle="--",
                    label=f"{BENCHMARK_LABEL}  {bench_final:+.1f}%", zorder=3)

        ax.axhline(0, color=MUTED, linewidth=0.8, alpha=0.5, zorder=1)

        ax.set_ylabel("Return since purchase", fontsize=7.5, color=MUTED)
        ax.tick_params(axis="both", labelsize=7, length=2, colors=MUTED)
        ax.yaxis.set_major_formatter(lambda v, _: f"{v:+.0f}%")
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=7))
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(mdates.AutoDateLocator()))

        ax.grid(True, axis="y", color=GRID, linewidth=0.7, zorder=0)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(GRID)
            ax.spines[side].set_linewidth(0.8)

        legend = ax.legend(fontsize=7, loc="best", frameon=True, framealpha=0.92)
        legend.get_frame().set_edgecolor(GRID)
        legend.get_frame().set_linewidth(0.6)

        title = job.get("title")
        if title:
            ax.set_title(title, fontsize=8.5, fontweight="bold", color=NAVY,
                         loc="left", pad=6)

        fig.tight_layout(pad=0.4)
        path = job["path"]
        fig.savefig(path, dpi=170, facecolor="white", bbox_inches="tight", pad_inches=0.04)
        return ticker, path

    except Exception as exc:  # noqa: BLE001 — one bad chart must not fail the report
        logger.warning("Chart render failed for %s: %s", ticker, exc)
        return ticker, None

    finally:
        # Closing in a finally, not after savefig: any exception raised
        # between subplots() and savefig() would otherwise leave the figure in
        # pyplot's global registry, and those accumulate across a long run
        # until the process is holding every failed chart it ever attempted.
        if fig is not None:
            plt.close(fig)


def build_charts(
    requests: Sequence[Tuple[str, str, Optional[str]]],
    prices: pd.DataFrame,
    cache_dir: Optional[Path] = None,
) -> Dict[Tuple[str, str], str]:
    """Render every requested chart, reusing anything already on disk.

    ``requests`` is a sequence of ``(ticker, start_date, title)``. Returns a
    mapping of ``(ticker, start_date)`` to a PNG path, omitting any chart that
    could not be produced.
    """
    cache_dir = cache_dir or CHART_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)

    if prices.empty or not requests:
        return {}

    through = pd.to_datetime(prices.index.max()).strftime("%Y-%m-%d")
    bench = prices[BENCHMARK_TICKER].dropna() if BENCHMARK_TICKER in prices.columns else None

    results: Dict[Tuple[str, str], str] = {}
    jobs: List[Dict] = []

    for ticker, start, title in requests:
        key = (ticker, start)
        if key in results or any(j["key"] == key for j in jobs):
            continue

        if ticker not in prices.columns:
            continue

        path = cache_dir / _cache_key(ticker, start, through)
        if path.exists() and path.stat().st_size > 0:
            results[key] = str(path)
            continue

        window = prices[ticker].loc[pd.Timestamp(start):].dropna()
        if len(window) < 2:
            continue

        bench_window = (
            bench.loc[pd.Timestamp(start):].dropna() if bench is not None else None
        )

        jobs.append(
            {
                "key": key,
                "ticker": ticker,
                "path": str(path),
                "title": title,
                "dates": [d.isoformat() for d in window.index],
                "values": window.tolist(),
                "bench_dates": (
                    [d.isoformat() for d in bench_window.index]
                    if bench_window is not None and len(bench_window) >= 2 else []
                ),
                "bench_values": (
                    bench_window.tolist()
                    if bench_window is not None and len(bench_window) >= 2 else []
                ),
            }
        )

    if results:
        logger.info("Reusing %s cached chart(s)", len(results))
    if not jobs:
        return results

    logger.info("Rendering %s chart(s) across %s workers", len(jobs), CHART_WORKERS)

    # A single figure is not worth a process pool's startup cost, and on a
    # constrained container the sequential path is the safer default.
    if len(jobs) == 1 or CHART_WORKERS <= 1:
        for job in jobs:
            _, path = _render_one(job)
            if path:
                results[job["key"]] = path
        return results

    try:
        with ProcessPoolExecutor(max_workers=min(CHART_WORKERS, len(jobs))) as pool:
            futures = {pool.submit(_render_one, job): job for job in jobs}
            for future in as_completed(futures):
                job = futures[future]
                try:
                    _, path = future.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Chart worker failed for %s: %s", job["ticker"], exc)
                    continue
                if path:
                    results[job["key"]] = path
    except Exception:
        # Process pools can be unavailable in constrained sandboxes.
        logger.warning("Parallel chart rendering unavailable — falling back to serial")

    # Serial pass over whatever is still missing — unconditionally, not only
    # when the pool failed to start. A worker dying mid-run raises
    # BrokenProcessPool from every remaining future, which the per-chart
    # handler above swallows one at a time; the pool then shuts down cleanly
    # and, in an earlier version, the fallback never ran and every chart was
    # silently lost. Rendering the leftovers here covers that case and the
    # partial-failure one in the same stroke.
    missing = [job for job in jobs if job["key"] not in results]
    if missing:
        logger.info("Rendering %s chart(s) serially", len(missing))
        for job in missing:
            _, path = _render_one(job)
            if path:
                results[job["key"]] = path

    return results


def prune_cache(keep_days: int = 30, cache_dir: Optional[Path] = None) -> int:
    """Delete cached PNGs untouched for ``keep_days``. Returns the count.

    The cache is keyed partly by the data's last date, so every trading day
    orphans yesterday's renders. Without this the volume grows without bound.
    """
    cache_dir = cache_dir or CHART_CACHE_DIR
    if not cache_dir.is_dir():
        return 0

    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).timestamp()
    removed = 0
    for png in cache_dir.glob("*.png"):
        try:
            if png.stat().st_mtime < cutoff:
                png.unlink()
                removed += 1
        except OSError as exc:
            logger.debug("Could not prune %s: %s", png, exc)

    if removed:
        logger.info("Pruned %s stale chart(s) from the cache", removed)
    return removed
