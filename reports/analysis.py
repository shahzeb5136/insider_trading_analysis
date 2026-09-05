"""Turns stored trades into the findings a briefing is built from.

Pure pandas — no network, no rendering, no I/O beyond reading the store. That
separation is what lets the report layout change without touching a single
number, and lets the numbers be checked without rendering a PDF.

The organising idea is that most insider activity is noise. Executives sell
constantly, on schedules set months ahead, for reasons that have nothing to do
with their view of the company. What carries signal is narrower:

* **Open-market purchases.** An insider buying with their own money at the
  market price, when they were under no obligation to.
* **Clusters.** Several insiders at the same company buying on the same day.
  Hard to explain as coincidence or personal liquidity.
* **Conviction relative to the person.** A $2M buy from a director who has
  never bought before reads differently from a $2M buy from someone who does
  it quarterly.

So the analysis leads with purchases and clusters, and treats the far larger
volume of sales and option exercises as context rather than headline.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from config import (
    CLUSTER_LOOKBACK_DAYS,
    LOOKBACK_DAYS,
    MIN_CLUSTER_INSIDERS,
    MIN_CLUSTER_VALUE_PER_INSIDER,
    MIN_TRADE_VALUE,
    PURCHASE_KEYWORDS,
)

logger = logging.getLogger(__name__)

# Trade types that represent a discretionary cash purchase. Option exercises
# and grants are deliberately excluded: they are compensation events, and
# counting them as "buying" is the most common way insider data is misread.
BUY_TYPES = ("Buy",)
SELL_TYPES = ("Sell",)


# ═══════════════════════════════════════════════════════════════════════════
# Findings
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class Cluster:
    """Several insiders buying the same stock on the same day."""

    ticker: str
    date: str
    insiders: int
    total_value: float
    total_shares: float
    trades: pd.DataFrame
    roles: List[str] = field(default_factory=list)
    chart_path: Optional[str] = None

    # Composition, used by the conviction score and shown in the spotlight.
    pct_direct: float = 0.0
    pct_c_suite: float = 0.0
    conviction: float = 0.0

    # Filled in by enrich_with_performance() once prices are available.
    since_return: Optional[float] = None
    bench_return: Optional[float] = None
    alpha: Optional[float] = None
    days_held: Optional[int] = None

    @property
    def is_recent(self) -> bool:
        """Within the current reporting window."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).date()
        return datetime.strptime(self.date, "%Y-%m-%d").date() >= cutoff

    @property
    def date_long(self) -> str:
        return fmt_date(self.date)


@dataclass
class Findings:
    """Everything the PDF renders, computed once."""

    as_of: str
    window_days: int
    window_start: str

    # Corpus-level
    total_trades: int
    total_tickers: int
    history_start: Optional[str]
    history_end: Optional[str]

    # Headline
    purchases: pd.DataFrame
    sales: pd.DataFrame
    purchase_value: float
    sale_value: float
    buy_sell_ratio: Optional[float]
    purchase_tickers: int

    # Signals
    clusters: List[Cluster]
    recent_clusters: List[Cluster]

    # Supporting tables
    top_buyers: pd.DataFrame
    repeat_buyers: pd.DataFrame
    largest_trades: pd.DataFrame
    type_breakdown: pd.DataFrame
    ticker_sentiment: pd.DataFrame

    # Selling splits into two very different things, and only the
    # discretionary half says anything about what an insider thinks of the
    # price today. Defaulted, so they trail every required field.
    scheduled_sale_value: float = 0.0
    discretionary_sale_value: float = 0.0

    def by_conviction(self, limit: Optional[int] = None) -> List[Cluster]:
        """Clusters ranked by conviction rather than by date."""
        ranked = sorted(self.clusters, key=lambda c: c.conviction, reverse=True)
        return ranked[:limit] if limit else ranked

    def chart_tickers(self) -> List[str]:
        """Every ticker needing a price series, deduplicated.

        Collected up front so the chart layer can issue one batched download
        instead of one per figure.
        """
        wanted = {c.ticker for c in self.clusters}
        if not self.purchases.empty:
            wanted.update(self.purchases["ticker"].unique().tolist())
        return sorted(wanted)

    @property
    def has_signal(self) -> bool:
        """True when the window contained anything worth leading with."""
        return not self.purchases.empty or bool(self.recent_clusters)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _is_open_market_purchase(frame: pd.DataFrame) -> pd.Series:
    """Rows that are genuine open-market buys.

    For SEC-sourced rows this is exact: transaction code ``P`` is the filer's
    own declaration that they bought on the open market. Nothing is inferred.

    Rows without a code — the legacy Yahoo-sourced ones — fall back to the old
    heuristic: the classifier said Buy *and* the filing text says "purchase".
    Both conditions, because that text is free-form and a handful of option
    acquisitions read as buys on wording alone.
    """
    if frame.empty:
        return pd.Series(dtype=bool)

    if "trans_code" in frame.columns:
        code = frame["trans_code"].astype("string").str.upper()
        by_code = code == "P"
        # Only fall back where there is genuinely no code to trust.
        needs_fallback = code.isna() | (code == "")
    else:
        by_code = pd.Series(False, index=frame.index)
        needs_fallback = pd.Series(True, index=frame.index)

    text = frame["text"].astype(str).str.lower()
    keyword_hit = pd.Series(False, index=frame.index)
    for keyword in PURCHASE_KEYWORDS:
        keyword_hit |= text.str.contains(keyword, regex=False, na=False)

    by_text = frame["trade_type"].isin(BUY_TYPES) & keyword_hit

    # Materialised as a plain bool. ``code == "P"`` on a nullable string
    # column yields pd.NA where the code is missing, and under three-valued
    # logic that NA survives the ``|`` — so the mask came back nullable and
    # ``astype(int)`` downstream raised on the very rows this fallback exists
    # for. A missing code that also fails the text test is simply "not a
    # purchase".
    return (by_code | (needs_fallback & by_text)).fillna(False).astype(bool)


def _is_scheduled(frame: pd.DataFrame) -> pd.Series:
    """Rows made under a pre-arranged Rule 10b5-1 plan.

    The distinction that makes selling readable. An executive disposing of
    stock under a plan adopted months earlier has expressed no view about
    today's price; one selling outside a plan has. Roughly a sixth of the
    sales in this dataset are scheduled, and treating them as equivalent is
    the most common way insider data misleads.
    """
    if frame.empty or "is_plan" not in frame.columns:
        return pd.Series(False, index=frame.index)
    return frame["is_plan"].fillna(False).astype(bool)


def _empty_like(columns: List[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=columns)


# Words that must stay upper-case through title-casing. ``str.title()`` would
# render these "Ceo", "Evp", "Ii" — which reads as a typo rather than a title.
_ACRONYMS = frozenset(
    """CEO CFO COO CTO CAO CMO CLO CIO CSO CCO CPO CHRO CRO CDO EVP SVP VP GC
       IT HR PR US UK EU IP AI ML VC PE REIT LLC LP LLP INC CO NA SA AG GMBH
       II III IV JR SR""".split()
)

# Short words that stay lower-case inside a title, unless they lead it.
_MINOR_WORDS = frozenset("and of the for to at in on a an & or".split())


def _title_case(text: str) -> str:
    """Title-case a shouted string without mangling acronyms or possessives.

    ``str.title()`` alone turns "PRESIDENT AND CEO" into "President And Ceo"
    and "DD'S DISCOUNTS" into "Dd'S Discounts". Both read as errors.
    """
    words = text.split()
    out: List[str] = []

    for index, word in enumerate(words):
        stripped = word.strip(",.&()-")
        if stripped.upper() in _ACRONYMS:
            out.append(word.upper())
            continue
        if index > 0 and stripped.lower() in _MINOR_WORDS:
            out.append(word.lower())
            continue
        # capitalize() rather than title(): title() capitalises after every
        # apostrophe, which is where "Dd'S" comes from.
        out.append(word.capitalize())

    return " ".join(out)


# Long phrases abbreviated *in place*, longest first so that "Chief Executive
# Officer" is matched before the bare "Officer" inside it.
_ROLE_ABBREVIATIONS: tuple[tuple[str, str], ...] = (
    ("beneficial owner of more than 10% of a class of security", "10% Owner"),
    ("executive vice president", "EVP"),
    ("chief executive officer", "CEO"),
    ("chief financial officer", "CFO"),
    ("chief operating officer", "COO"),
    ("chief technology officer", "CTO"),
    ("chief accounting officer", "CAO"),
    ("chief marketing officer", "CMO"),
    ("chief medical officer", "CMO"),
    ("chief legal officer", "CLO"),
    ("chief investment officer", "CIO"),
    ("chief information officer", "CIO"),
    ("chief scientific officer", "CSO"),
    ("chief commercial officer", "CCO"),
    ("chief people officer", "CPO"),
    ("chief human resources officer", "CHRO"),
    ("senior vice president", "SVP"),
    ("vice president", "VP"),
    ("board of directors", "Board"),
    (" and ", " & "),
)


def _shorten_role(position: Any) -> str:
    """Compress a filing's job title into something that fits a table cell.

    Abbreviates in place rather than substituting a label for the whole
    string. That distinction matters: an earlier version matched substrings
    and returned a fixed word, so "President, CEO & Director" came back as
    just "Director" — dropping the part a reader actually cares about — and
    "President and CEO" came back as the meaningless "Pres. &".

    Filings also arrive in inconsistent case ("PRESIDENT" beside "President"),
    so anything shouting is title-cased to stop the same job reading as two.
    """
    text = str(position or "").strip()
    if not text:
        return "—"

    # ALL CAPS is a filing-style artifact, not emphasis.
    if text.isupper():
        text = _title_case(text)

    lowered = text.lower()
    for needle, short in _ROLE_ABBREVIATIONS:
        if needle in lowered:
            # Rebuild case-insensitively while preserving the rest verbatim.
            index = lowered.index(needle)
            text = text[:index] + short + text[index + len(needle):]
            lowered = text.lower()

    text = " ".join(text.split())
    return text if len(text) <= 30 else text[:29] + "…"


_PRICE_RE = re.compile(r"at price\s+([\d,]+\.?\d*)(?:\s*-\s*([\d,]+\.?\d*))?", re.I)

# Titles that mark someone with a view of the whole business rather than one
# function. A CEO buying is a different statement from a divisional VP buying.
_C_SUITE_MARKERS = (
    "chief executive", "chief financial", "chief operating", "president",
    "chairman", "chair of", "chief investment",
)


def parse_price(text: Any) -> Optional[float]:
    """Pull the execution price out of a filing description.

    Filing text is formulaic — ``Sale at price 390.01 per share.`` or
    ``Sale at price 525.00 - 529.73 per share.`` — so the price is the only
    part of that column carrying information the other columns do not. Once
    it is extracted the whole free-text column can be dropped, which is what
    lets the tables fit at a readable size.

    Ranges collapse to their midpoint. Anything unparseable returns None
    rather than raising: the column is a nicety, not a requirement.
    """
    match = _PRICE_RE.search(str(text or ""))
    if not match:
        return None
    try:
        low = float(match.group(1).replace(",", ""))
        high = float(match.group(2).replace(",", "")) if match.group(2) else low
    except (ValueError, AttributeError):
        return None
    return (low + high) / 2.0


def _clean_detail(text: Any) -> str:
    """Trim a filing description down to its substance.

    The strings are formulaic — "Purchase at price 41.55 per share." — so the
    price is the only part worth keeping in a dense table.
    """
    raw = str(text or "").strip()
    if not raw:
        return "—"
    raw = raw.rstrip(".")
    return raw if len(raw) <= 60 else raw[:59] + "…"


# ═══════════════════════════════════════════════════════════════════════════
# Cluster detection
# ═══════════════════════════════════════════════════════════════════════════


def find_clusters(
    frame: pd.DataFrame,
    min_insiders: int = MIN_CLUSTER_INSIDERS,
    min_value: float = MIN_CLUSTER_VALUE_PER_INSIDER,
    lookback_days: int = CLUSTER_LOOKBACK_DAYS,
) -> List[Cluster]:
    """Find same-day, same-ticker, multi-insider purchase clusters.

    Ordered newest first, then by size. A cluster from two years ago is
    historically interesting but is not what a daily briefing leads with, so
    ``lookback_days`` bounds how far back the section reaches (0 disables the
    bound).
    """
    if frame.empty:
        return []

    buys = frame[_is_open_market_purchase(frame)]
    buys = buys[buys["abs_value"] >= min_value]

    if lookback_days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        buys = buys[buys["trade_date"] >= cutoff]

    if buys.empty:
        return []

    clusters: List[Cluster] = []
    for (trade_date, ticker), group in buys.groupby(["trade_date", "ticker"], sort=False):
        distinct = group["insider"].nunique()
        if distinct < min_insiders:
            continue

        ordered = group.sort_values("abs_value", ascending=False)
        clusters.append(
            Cluster(
                ticker=str(ticker),
                date=str(trade_date),
                insiders=int(distinct),
                total_value=float(ordered["abs_value"].sum()),
                total_shares=float(ordered["shares"].fillna(0).sum()),
                trades=ordered,
                roles=[_shorten_role(p) for p in ordered["position"].head(4)],
            )
        )

    for cluster in clusters:
        _score_cluster(cluster)

    clusters.sort(key=lambda c: (c.date, c.total_value), reverse=True)
    return clusters


def _score_cluster(cluster: Cluster) -> None:
    """Attach a 0-1 conviction score, in place.

    Chronological order is the wrong way to present clusters — the most
    recent is not the most interesting. This is a weighted composite in the
    same shape the dip and stable-growth analysers use, over the five things
    that separate a strong cluster from a technically-qualifying one:

    * **How many insiders** (30%). Two is the threshold; four is a statement.
    * **How much money** (25%), on a log scale, so $10M and $100M are
      distinguishable without $1B swamping everything else.
    * **Direct ownership** (20%). Shares held outright, rather than through a
      trust or partnership, means the buyer personally carries the outcome.
    * **Seniority** (15%). A CEO and CFO buying together see the whole
      business; two divisional VPs see their division.
    * **Recency** (10%), decaying over a year. Old clusters stay in the
      record but should not lead it.
    """
    frame = cluster.trades

    ownership = frame["ownership"].astype(str).str.upper().str.strip()
    cluster.pct_direct = float((ownership == "D").mean()) if len(frame) else 0.0

    positions = frame["position"].astype(str).str.lower()
    is_senior = positions.apply(
        lambda p: any(marker in p for marker in _C_SUITE_MARKERS)
    )
    cluster.pct_c_suite = float(is_senior.mean()) if len(frame) else 0.0

    try:
        age_days = (
            datetime.now(timezone.utc).date()
            - datetime.strptime(cluster.date, "%Y-%m-%d").date()
        ).days
    except ValueError:
        age_days = 365

    cluster.conviction = (
        0.30 * min(cluster.insiders / 4.0, 1.0)
        + 0.25 * min(math.log10(max(cluster.total_value, 1.0)) / 9.0, 1.0)
        + 0.20 * cluster.pct_direct
        + 0.15 * cluster.pct_c_suite
        + 0.10 * math.exp(-max(age_days, 0) / 365.0)
    )


def enrich_with_performance(
    clusters: List[Cluster],
    prices: pd.DataFrame,
    benchmark: str,
) -> None:
    """Attach since-purchase performance to each cluster, in place.

    This is the number that turns a list of clusters into a track record.
    Without it the reader has to eyeball a chart to answer "was the insider
    right?"; with it the league table answers it for every cluster at once,
    including the ones that never get a chart.

    Alpha is the simple difference in total return over the same window, in
    percentage points — not a risk-adjusted measure, and labelled as such
    wherever it is shown.
    """
    if prices is None or prices.empty:
        return

    bench_series = prices[benchmark].dropna() if benchmark in prices.columns else None

    for cluster in clusters:
        if cluster.ticker not in prices.columns:
            continue

        try:
            start = pd.Timestamp(cluster.date)
            window = prices[cluster.ticker].loc[start:].dropna()
            if len(window) < 2:
                continue

            cluster.since_return = float((window.iloc[-1] / window.iloc[0] - 1.0) * 100.0)
            cluster.days_held = int((window.index[-1] - window.index[0]).days)

            if bench_series is not None:
                bench_window = bench_series.loc[start:].dropna()
                if len(bench_window) >= 2:
                    cluster.bench_return = float(
                        (bench_window.iloc[-1] / bench_window.iloc[0] - 1.0) * 100.0
                    )
                    cluster.alpha = cluster.since_return - cluster.bench_return
        except (KeyError, IndexError, ValueError, ZeroDivisionError) as exc:
            logger.debug("Could not score %s cluster of %s: %s", cluster.ticker, cluster.date, exc)


# ═══════════════════════════════════════════════════════════════════════════
# Supporting tables
# ═══════════════════════════════════════════════════════════════════════════


def _top_buyers(purchases: pd.DataFrame, limit: int = 12) -> pd.DataFrame:
    """Who put the most money to work in the window."""
    if purchases.empty:
        return _empty_like(["insider", "ticker", "position", "trades", "total_value"])

    grouped = (
        purchases.groupby(["insider", "ticker"], as_index=False)
        .agg(
            position=("position", "first"),
            trades=("trade_key", "count"),
            total_value=("abs_value", "sum"),
        )
        .sort_values("total_value", ascending=False)
        .head(limit)
    )
    grouped["position"] = grouped["position"].map(_shorten_role)
    return grouped.reset_index(drop=True)


def _repeat_buyers(purchases: pd.DataFrame, limit: int = 10) -> pd.DataFrame:
    """Insiders who bought the same name on more than one day.

    Adding to a position is a stronger statement than opening one, because
    the second purchase is made with the first already marked to market.
    """
    if purchases.empty:
        return _empty_like(["insider", "ticker", "days", "trades", "total_value", "dates"])

    grouped = (
        purchases.groupby(["insider", "ticker"], as_index=False)
        .agg(
            days=("trade_date", "nunique"),
            trades=("trade_key", "count"),
            total_value=("abs_value", "sum"),
            dates=("trade_date", lambda s: ", ".join(sorted(set(s))[:4])),
        )
    )
    grouped = grouped[grouped["days"] > 1].sort_values("total_value", ascending=False)
    return grouped.head(limit).reset_index(drop=True)


def _type_breakdown(window: pd.DataFrame) -> pd.DataFrame:
    """Every trade type in the window, by count and value."""
    if window.empty:
        return _empty_like(["trade_type", "count", "total_value", "avg_value"])

    grouped = (
        window.groupby("trade_type", as_index=False)
        .agg(count=("trade_key", "count"), total_value=("abs_value", "sum"))
        .sort_values("total_value", ascending=False)
    )
    grouped["avg_value"] = grouped["total_value"] / grouped["count"]
    return grouped.reset_index(drop=True)


def _ticker_sentiment(window: pd.DataFrame, limit: int = 14) -> pd.DataFrame:
    """Net buy-versus-sell pressure per ticker, ranked by conviction.

    Tickers with genuine purchases sort above pure-selling names regardless of
    dollar size, because a $200M scheduled disposal says less than a $2M buy.
    """
    if window.empty:
        return _empty_like(["ticker", "bought", "sold", "net", "buy_trades", "sell_trades"])

    is_buy = _is_open_market_purchase(window)
    is_sell = window["trade_type"].isin(SELL_TYPES)

    frame = window.assign(
        _bought=window["abs_value"].where(is_buy, 0.0),
        _sold=window["abs_value"].where(is_sell, 0.0),
        _buy_n=is_buy.astype(int),
        _sell_n=is_sell.astype(int),
    )

    grouped = (
        frame.groupby("ticker", as_index=False)
        .agg(
            bought=("_bought", "sum"),
            sold=("_sold", "sum"),
            buy_trades=("_buy_n", "sum"),
            sell_trades=("_sell_n", "sum"),
        )
    )
    grouped["net"] = grouped["bought"] - grouped["sold"]
    grouped["_has_buy"] = (grouped["bought"] > 0).astype(int)

    grouped = grouped.sort_values(
        ["_has_buy", "bought", "sold"], ascending=[False, False, False]
    ).drop(columns="_has_buy")

    return grouped.head(limit).reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════


def analyse(
    frame: pd.DataFrame,
    as_of: Optional[datetime] = None,
    window_days: int = LOOKBACK_DAYS,
) -> Findings:
    """Compute every finding the report needs from the full trade history.

    ``frame`` is the whole store: clusters look further back than the daily
    window, so the caller must not pre-filter.
    """
    as_of = as_of or datetime.now(timezone.utc)
    window_start = (as_of - timedelta(days=window_days)).strftime("%Y-%m-%d")
    as_of_str = as_of.strftime("%Y-%m-%d")

    if frame.empty:
        logger.warning("Analysing an empty trade store")
        empty = _empty_like(["ticker", "insider", "position", "trade_date", "abs_value"])
        return Findings(
            as_of=as_of_str, window_days=window_days, window_start=window_start,
            total_trades=0, total_tickers=0, history_start=None, history_end=None,
            purchases=empty, sales=empty, purchase_value=0.0, sale_value=0.0,
            buy_sell_ratio=None, purchase_tickers=0, clusters=[], recent_clusters=[],
            top_buyers=empty, repeat_buyers=empty, largest_trades=empty,
            type_breakdown=empty, ticker_sentiment=empty,
        )

    # SEC rows carry the execution price as a field. Legacy Yahoo rows only
    # have it buried in prose, so it is parsed out for those. Done on the full
    # frame rather than the window because clusters reach back across the
    # whole history and their participant tables need the price too.
    frame = frame.copy()
    if "price" in frame.columns:
        # Coerced to a nullable float first. Writing the parsed result into a
        # float64 column with .loc raises under pandas 3 when the parse comes
        # back all-None ("Invalid value '[None]' for dtype 'float64'") — which
        # is precisely the mixed case that occurs in practice, where SEC rows
        # carry a price and an older row does not.
        parsed = pd.to_numeric(
            frame["text"].map(parse_price), errors="coerce"
        ).astype("float64")
        frame["price"] = pd.to_numeric(frame["price"], errors="coerce").fillna(parsed)
    else:
        frame["price"] = pd.to_numeric(
            frame["text"].map(parse_price), errors="coerce"
        ).astype("float64")

    window = frame[frame["trade_date"] >= window_start].copy()

    purchases = window[_is_open_market_purchase(window)].sort_values(
        "abs_value", ascending=False
    )
    sales = window[window["trade_type"].isin(SELL_TYPES)].sort_values(
        "abs_value", ascending=False
    )

    purchase_value = float(purchases["abs_value"].sum()) if not purchases.empty else 0.0
    sale_value = float(sales["abs_value"].sum()) if not sales.empty else 0.0
    ratio = (purchase_value / sale_value) if sale_value > 0 else None

    if sales.empty:
        scheduled_value = discretionary_value = 0.0
    else:
        scheduled = _is_scheduled(sales)
        scheduled_value = float(sales.loc[scheduled, "abs_value"].sum())
        discretionary_value = float(sales.loc[~scheduled, "abs_value"].sum())

    clusters = find_clusters(frame)

    findings = Findings(
        as_of=as_of_str,
        window_days=window_days,
        window_start=window_start,
        total_trades=int(len(frame)),
        total_tickers=int(frame["ticker"].nunique()),
        history_start=str(frame["trade_date"].min()),
        history_end=str(frame["trade_date"].max()),
        purchases=purchases,
        sales=sales,
        purchase_value=purchase_value,
        sale_value=sale_value,
        buy_sell_ratio=ratio,
        purchase_tickers=int(purchases["ticker"].nunique()) if not purchases.empty else 0,
        scheduled_sale_value=scheduled_value,
        discretionary_sale_value=discretionary_value,
        clusters=clusters,
        recent_clusters=[c for c in clusters if c.is_recent],
        top_buyers=_top_buyers(purchases),
        repeat_buyers=_repeat_buyers(purchases),
        largest_trades=window.nlargest(12, "abs_value") if not window.empty else window,
        type_breakdown=_type_breakdown(window),
        ticker_sentiment=_ticker_sentiment(window),
    )

    logger.info(
        "Analysis: %s purchases (%s) across %s tickers, %s clusters (%s recent), "
        "%s sales (%s) in the %s-day window",
        len(purchases), fmt_money(purchase_value), findings.purchase_tickers,
        len(clusters), len(findings.recent_clusters),
        len(sales), fmt_money(sale_value), window_days,
    )
    return findings


def narrative(findings: Findings) -> List[str]:
    """Three to six sentences summarising the window, for page one.

    Written to survive a quiet week. Insider purchasing is genuinely sparse —
    267 open-market buys across two and a half years in this dataset — so the
    common case is a window with no purchases at all. "No purchases found" is
    a true statement and a useless one; these fallbacks say what *did* happen
    and when the last real signal was, so the page always carries information.
    """
    lines: List[str] = []
    window = f"the last {findings.window_days} days"

    if not findings.purchases.empty:
        top = findings.purchases.iloc[0]
        lines.append(
            f"Insiders bought {fmt_money(findings.purchase_value)} across "
            f"{len(findings.purchases)} open-market purchases in {window}, "
            f"spanning {findings.purchase_tickers} "
            f"{'company' if findings.purchase_tickers == 1 else 'companies'}."
        )
        lines.append(
            f"The largest was {top['insider'].title()} "
            f"({_shorten_role(top['position'])}) buying "
            f"{fmt_money(top['abs_value'])} of {top['ticker']} on "
            f"{fmt_date(top['trade_date'])}."
        )
    else:
        lines.append(
            f"No qualifying open-market purchases were filed in {window}."
        )
        recent_buys = None
        if findings.clusters:
            recent_buys = findings.clusters[0].date
        if recent_buys:
            lines.append(
                f"The most recent multi-insider cluster was "
                f"{findings.clusters[0].ticker} on {fmt_date(recent_buys)}."
            )

    if findings.sale_value > 0:
        if findings.purchase_value > 0 and findings.buy_sell_ratio is not None:
            lines.append(
                f"Selling ran to {fmt_money(findings.sale_value)}, so the tape "
                f"was {findings.buy_sell_ratio:.2f}:1 buys to sells by value."
            )
        else:
            lines.append(
                f"The window was entirely disposals — {fmt_money(findings.sale_value)} "
                f"across {len(findings.sales)} sales."
            )

        # The share of selling that was pre-arranged is what separates
        # "executives are bailing out" from "a calendar fired".
        if findings.scheduled_sale_value > 0:
            share = findings.scheduled_sale_value / findings.sale_value
            lines.append(
                f"{share:.0%} of that selling ({fmt_money(findings.scheduled_sale_value)}) "
                f"was pre-arranged under Rule 10b5-1 plans, leaving "
                f"{fmt_money(findings.discretionary_sale_value)} discretionary."
            )

    if findings.recent_clusters:
        best = max(findings.recent_clusters, key=lambda c: c.conviction)
        lines.append(
            f"New cluster signal: {best.insiders} insiders bought "
            f"{fmt_money(best.total_value)} of {best.ticker} on "
            f"{best.date_long}."
        )
    elif findings.clusters:
        best = findings.by_conviction(1)[0]
        performance = ""
        if best.alpha is not None:
            performance = (
                f", now {best.since_return:+.1f}% "
                f"({best.alpha:+.1f} pp vs the index)"
            )
        lines.append(
            f"No new clusters. The strongest on record remains {best.ticker} "
            f"from {best.date_long}{performance}."
        )

    return lines


# ═══════════════════════════════════════════════════════════════════════════
# Formatting
# ═══════════════════════════════════════════════════════════════════════════


def fmt_money(value: Any, precise: bool = False) -> str:
    """Format a dollar amount at the precision a reader can actually use.

    $1,247,000,000 is harder to read than $1.25B, and in a table of trades the
    extra digits carry no information anyone acts on.
    """
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "—"

    sign = "-" if amount < 0 else ""
    amount = abs(amount)

    if precise:
        return f"{sign}${amount:,.0f}"
    if amount >= 1_000_000_000:
        return f"{sign}${amount / 1_000_000_000:,.2f}B"
    if amount >= 1_000_000:
        return f"{sign}${amount / 1_000_000:,.1f}M"
    if amount >= 1_000:
        return f"{sign}${amount / 1_000:,.0f}K"
    return f"{sign}${amount:,.0f}"


def fmt_shares(value: Any) -> str:
    """Share counts, abbreviated the same way."""
    try:
        count = float(value)
    except (TypeError, ValueError):
        return "—"
    if pd.isna(count):
        return "—"

    if abs(count) >= 1_000_000:
        return f"{count / 1_000_000:,.2f}M"
    if abs(count) >= 1_000:
        return f"{count / 1_000:,.1f}K"
    return f"{count:,.0f}"


def fmt_date(value: Any) -> str:
    """``2026-09-05`` → ``05 Sep 2026``."""
    try:
        return pd.to_datetime(value).strftime("%d %b %Y")
    except Exception:  # noqa: BLE001
        return str(value)
