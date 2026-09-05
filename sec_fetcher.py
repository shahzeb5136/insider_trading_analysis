"""Insider transactions from SEC EDGAR — the authoritative source.

Two paths, because EDGAR publishes the same data in two shapes and each is
right for a different job:

    cold store  ──►  quarterly bulk ZIPs        ~10MB each, parses in ~1s
                     (SUBMISSION + REPORTINGOWNER + NONDERIV_TRANS)

    daily run   ──►  daily form index           1 request per calendar day
                     └─► Form 4 XML for issuers we care about   ~180 requests

The bulk datasets are published a quarter in arrears, so they cannot serve the
current window; the daily index covers everything since. Together they give
full history and same-day freshness for roughly 200 requests a day.

Why this replaces the Yahoo path
--------------------------------
Yahoo's insider feed is a convenience endpoint, and it shows: history is
hard-capped at 150 rows per ticker, the feed runs a median ~18 days behind
(XOM was measured 173 days stale), and it was missing $55M of Cascade
Investment buying in RSG that EDGAR had carried since the filing date.

EDGAR also answers a question Yahoo cannot. Every transaction carries the
filer's own **transaction code** — ``P`` for an open-market purchase, ``S``
for a sale, ``M`` for an option exercise, ``A`` for a grant. The old pipeline
had to infer that by grepping free text for the word "purchase", which is
both fragile and wrong at the edges. And ``aff10b5One`` states outright
whether a sale was made under a pre-arranged 10b5-1 plan, which is exactly
the distinction between a scheduled disposal and a decision.

Rate limiting follows SEC's published policy: a declared User-Agent carrying
a real contact address, and no more than 10 requests a second. This module
runs at 8.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
from xml.etree import ElementTree

import store
from config import (
    MIN_TRADE_VALUE,
    SEC_BACKFILL_QUARTERS,
    SEC_GAP_DAYS_PER_RUN,
    SEC_LOOKBACK_DAYS,
    SEC_MAX_RETRIES,
    SEC_RATE_PER_SECOND,
    SEC_USER_AGENT,
    SEC_WORKERS,
)
from fetcher import TokenBucket, get_sp500_tickers

logger = logging.getLogger(__name__)

_BULK_URL = (
    "https://www.sec.gov/files/structureddata/data/"
    "insider-transactions-data-sets/{quarter}_form345.zip"
)
_DAILY_INDEX_URL = (
    "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{qtr}/form.{stamp}.idx"
)
_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_ARCHIVE_ROOT = "https://www.sec.gov/Archives/"

# ── SEC transaction codes ────────────────────────────────────────────────────
# The full table is in the Form 4 instructions. Only a handful matter here,
# but the mapping is exhaustive so an unfamiliar code is labelled rather than
# silently bucketed as a purchase.
_TRANS_CODES: Dict[str, str] = {
    "P": "Buy",                    # Open-market or private purchase
    "S": "Sell",                   # Open-market or private sale
    "A": "Grant/Award",            # Grant, award or other acquisition from the issuer
    "D": "Grant/Award",            # Disposition to the issuer
    "F": "Tax Withholding",        # Shares withheld to pay tax or exercise price
    "M": "Exercise/Conversion",    # Exercise or conversion of a derivative
    "C": "Exercise/Conversion",    # Conversion of a derivative security
    "X": "Exercise/Conversion",    # Exercise of an in-the-money derivative
    "G": "Gift",                   # Bona fide gift
    "V": "Other",                  # Transaction voluntarily reported early
    "J": "Other",                  # Other acquisition or disposition
    "K": "Other",                  # Equity swap or similar
    "U": "Other",                  # Disposition in a tender of shares
    "W": "Other",                  # Acquisition or disposition by will or inheritance
    "H": "Other",
    "I": "Other",
    "L": "Other",
    "O": "Exercise/Conversion",
    "E": "Other",
}


def _http_get(url: str, timeout: int = 45) -> bytes:
    """One GET with SEC's required User-Agent."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": SEC_USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
            "Host": "www.sec.gov",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            import gzip

            payload = gzip.decompress(payload)
        return payload


def _http_get_retrying(
    url: str, bucket: Optional[TokenBucket] = None, timeout: int = 45
) -> Optional[bytes]:
    """GET with the rate limit applied and a bounded retry.

    Returns None rather than raising on a 404 — a missing daily index simply
    means the market was closed that day, which is the common case for two
    days of every seven.
    """
    for attempt in range(1, SEC_MAX_RETRIES + 1):
        if bucket:
            bucket.take()
        try:
            return _http_get(url, timeout=timeout)
        except urllib.error.HTTPError as exc:
            # 403 as well as 404: EDGAR answers 403 for an index file that
            # does not exist yet, which is what today's index looks like
            # before the day's filings are published.
            if exc.code in (403, 404):
                return None
            if exc.code in (429, 503) and attempt < SEC_MAX_RETRIES:
                wait = 2.0 * attempt
                logger.warning("SEC returned %s for %s — backing off %.0fs", exc.code, url, wait)
                time.sleep(wait)
                continue
            logger.warning("SEC HTTP %s for %s", exc.code, url)
            return None
        except Exception as exc:  # noqa: BLE001
            if attempt < SEC_MAX_RETRIES:
                time.sleep(1.5 * attempt)
                continue
            logger.warning("SEC request failed for %s: %s", url, exc)
            return None
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Ticker ↔ CIK
# ═══════════════════════════════════════════════════════════════════════════


def load_cik_map(tickers: Sequence[str]) -> Dict[str, str]:
    """Map the S&P 500 tickers to zero-padded issuer CIKs.

    This is what makes the daily path cheap. The index lists ~2,000 Form 4s a
    day across the whole market; knowing which CIKs are in the index means
    fetching only the ~180 that belong to companies we cover, instead of
    every one of them to find out.
    """
    payload = _http_get_retrying(_COMPANY_TICKERS_URL)
    if not payload:
        logger.error("Could not load SEC company_tickers.json")
        return {}

    try:
        raw = json.loads(payload)
    except json.JSONDecodeError:
        logger.exception("company_tickers.json did not parse")
        return {}

    # EDGAR spells class shares with a hyphen (BRK-B); the constituent lists
    # we read use the same convention, so normalise both to be safe.
    wanted = {t.replace(".", "-").upper() for t in tickers}

    mapping: Dict[str, str] = {}
    for entry in raw.values():
        symbol = str(entry.get("ticker", "")).replace(".", "-").upper()
        if symbol in wanted:
            mapping[f"{int(entry['cik_str']):010d}"] = symbol

    missing = wanted - set(mapping.values())
    if missing:
        logger.info(
            "%s ticker(s) had no CIK in company_tickers.json: %s",
            len(missing), ", ".join(sorted(missing)[:12]),
        )

    logger.info("Mapped %s tickers to CIKs", len(mapping))
    return mapping


# ═══════════════════════════════════════════════════════════════════════════
# Row construction
# ═══════════════════════════════════════════════════════════════════════════


def _describe(code: str, shares: float, price: float, is_plan: bool) -> str:
    """A human-readable description, in the shape the report expects.

    The report renders this as the filing's detail line, and the analysis
    parses a price out of it for older Yahoo-sourced rows. Writing it in the
    same grammar keeps one code path for both sources.
    """
    label = {
        "P": "Purchase", "S": "Sale", "A": "Grant or award",
        "M": "Exercise of derivative security", "C": "Conversion of derivative security",
        "X": "Exercise of in-the-money derivative", "G": "Bona fide gift",
        "F": "Shares withheld for tax", "D": "Disposition to issuer",
    }.get(code, f"Transaction (code {code})")

    if price and price > 0:
        text = f"{label} at price {price:,.2f} per share."
    else:
        text = f"{label}."

    if is_plan:
        text += " Made under a Rule 10b5-1 trading plan."
    return text


def _relationship(row: Dict[str, Any]) -> str:
    """Normalise the filer's stated relationship into a job title."""
    title = (row.get("title") or "").strip()
    if title:
        return title

    flags = []
    if row.get("is_officer"):
        flags.append("Officer")
    if row.get("is_director"):
        flags.append("Director")
    if row.get("is_ten_percent"):
        flags.append("Beneficial Owner of more than 10% of a Class of Security")
    if row.get("is_other"):
        flags.append("Other")
    return ", ".join(flags) if flags else "Insider"


def _build_row(
    ticker: str,
    insider: str,
    position: str,
    trade_date: str,
    code: str,
    shares: Optional[float],
    price: Optional[float],
    acquired_disposed: str,
    ownership: str,
    is_plan: bool,
    accession: str,
) -> Optional[Dict[str, Any]]:
    """Turn one parsed SEC transaction into a store row.

    Returns None for anything below the reporting threshold, which is most
    of them — the bulk feed carries every Form 4 filed by every issuer.
    """
    if not shares or price is None:
        return None

    value = float(shares) * float(price)
    if abs(value) < MIN_TRADE_VALUE:
        return None

    # SEC signs by direction: A(cquired) adds to the holding, D(isposed)
    # reduces it. Store the value signed the same way, so a sale is negative
    # exactly as the Yahoo-sourced rows were.
    if str(acquired_disposed).upper() == "D":
        value = -value

    trade_type = _TRANS_CODES.get(str(code).upper(), "Other")
    text = _describe(str(code).upper(), float(shares), float(price), is_plan)

    return {
        "trade_key": store._trade_key(ticker, insider, trade_date, shares, value, text),
        "ticker": ticker.upper(),
        "insider": insider.strip(),
        "position": position.strip(),
        "trade_date": trade_date,
        "trade_type": trade_type,
        "text": text,
        "transaction_desc": f"{code} / {accession}",
        "shares": float(shares),
        "price": float(price),
        "value": value,
        "abs_value": abs(value),
        "ownership": str(ownership).upper()[:1] or "D",
        "trans_code": str(code).upper()[:2],
        "is_plan": 1 if is_plan else 0,
        "source": "sec",
        "first_seen": datetime.now(timezone.utc).isoformat(),
    }


# Form 4s are self-reported and filers do mistype dates. A handful of rows in
# every backfill carry years like 0024 (for 2024) or dates a year or two in
# the future. There are only a few — 4 in 28,000 on the last full load — but
# they are pure poison for a report that sorts by date: a 2028 transaction
# sits at the top of "most recent" permanently, and an 0024 one silently
# becomes the start of the reported history.
_EARLIEST_PLAUSIBLE = date(1990, 1, 1)


def _to_iso(value: str) -> Optional[str]:
    """Parse an SEC date to ``YYYY-MM-DD``, rejecting implausible ones.

    SEC bulk dates are ``31-MAR-2026``; Form 4 XML dates are already ISO.
    Anything before 1990 or more than two days ahead of now is treated as a
    filing error and dropped — two days rather than zero because filings
    arrive from every US timezone and a same-day transaction can legitimately
    read as tomorrow in UTC.
    """
    text = (value or "").strip()
    if not text:
        return None

    parsed: Optional[date] = None
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%B-%Y"):
        try:
            parsed = datetime.strptime(text, fmt).date()
            break
        except ValueError:
            continue

    if parsed is None:
        return None

    horizon = datetime.now(timezone.utc).date() + timedelta(days=2)
    if parsed < _EARLIEST_PLAUSIBLE or parsed > horizon:
        logger.debug("Dropping implausible transaction date %r", text)
        return None

    return parsed.strftime("%Y-%m-%d")


def _number(value: Any) -> Optional[float]:
    try:
        text = str(value).strip().replace(",", "")
        return float(text) if text else None
    except (TypeError, ValueError):
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Bulk backfill
# ═══════════════════════════════════════════════════════════════════════════


def _recent_quarters(count: int, today: Optional[date] = None) -> List[str]:
    """The last ``count`` published quarters, newest first.

    The current quarter is skipped: SEC publishes each dataset after the
    quarter closes, so requesting it returns a 404.
    """
    today = today or datetime.now(timezone.utc).date()
    year, quarter = today.year, (today.month - 1) // 3 + 1

    quarters: List[str] = []
    for _ in range(count):
        quarter -= 1
        if quarter == 0:
            quarter = 4
            year -= 1
        quarters.append(f"{year}q{quarter}")
    return quarters


def _newest_published_quarter(today: Optional[date] = None) -> Optional[str]:
    """The most recent quarter whose bulk dataset actually exists.

    SEC publishes each dataset well after the quarter closes, and the lag is
    not fixed — as of this writing 2026q1 is available while 2026q2 is not.
    Assuming "last completed quarter" would put the start of the gap three
    months too late and silently leave that quarter missing from the store,
    which is exactly the failure this function exists to prevent. One or two
    HEAD requests settle it for certain.
    """
    for quarter in _recent_quarters(6, today):
        url = _BULK_URL.format(quarter=quarter)
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": SEC_USER_AGENT}, method="HEAD"
            )
            with urllib.request.urlopen(request, timeout=30):
                logger.debug("Newest published bulk quarter: %s", quarter)
                return quarter
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                continue
            logger.warning("Probing %s returned HTTP %s", quarter, exc.code)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not probe %s: %s", quarter, exc)
            return None
    return None


def _parse_bulk_zip(payload: bytes, cik_to_ticker: Dict[str, str]) -> List[Dict[str, Any]]:
    """Extract qualifying transactions from one quarterly dataset.

    The three tables join on ACCESSION_NUMBER: SUBMISSION carries the issuer
    and the 10b5-1 flag, REPORTINGOWNER the filer, NONDERIV_TRANS the trades.
    """
    archive = zipfile.ZipFile(io.BytesIO(payload))

    def read(name: str) -> List[Dict[str, str]]:
        with archive.open(name) as handle:
            text = io.TextIOWrapper(handle, encoding="latin-1", newline="")
            return list(csv.DictReader(text, delimiter="\t"))

    try:
        submissions = read("SUBMISSION.tsv")
        owners = read("REPORTINGOWNER.tsv")
        transactions = read("NONDERIV_TRANS.tsv")
    except KeyError:
        logger.warning("Bulk archive is missing an expected table")
        return []

    wanted_tickers = set(cik_to_ticker.values())

    # accession -> (ticker, is_plan)
    issuers: Dict[str, Tuple[str, bool]] = {}
    for row in submissions:
        symbol = (row.get("ISSUERTRADINGSYMBOL") or "").strip().replace(".", "-").upper()
        if symbol and symbol in wanted_tickers:
            issuers[row["ACCESSION_NUMBER"]] = (
                symbol,
                (row.get("AFF10B5ONE") or "").strip().lower() == "true",
            )

    # accession -> (name, relationship)
    filers: Dict[str, Tuple[str, str]] = {}
    for row in owners:
        accession = row["ACCESSION_NUMBER"]
        if accession not in issuers or accession in filers:
            continue
        relationship = (row.get("RPTOWNER_TITLE") or "").strip()
        if not relationship:
            relationship = (row.get("RPTOWNER_RELATIONSHIP") or "").strip() or "Insider"
        filers[accession] = ((row.get("RPTOWNERNAME") or "").strip(), relationship)

    rows: List[Dict[str, Any]] = []
    for row in transactions:
        accession = row["ACCESSION_NUMBER"]
        issuer = issuers.get(accession)
        if not issuer:
            continue

        ticker, is_plan = issuer
        insider, position = filers.get(accession, ("Unknown", "Insider"))
        trade_date = _to_iso(row.get("TRANS_DATE", ""))
        if not trade_date:
            continue

        built = _build_row(
            ticker=ticker,
            insider=insider,
            position=position,
            trade_date=trade_date,
            code=row.get("TRANS_CODE", ""),
            shares=_number(row.get("TRANS_SHARES")),
            price=_number(row.get("TRANS_PRICEPERSHARE")),
            acquired_disposed=row.get("TRANS_ACQUIRED_DISP_CD", "A"),
            ownership=row.get("DIRECT_INDIRECT_OWNERSHIP", "D"),
            is_plan=is_plan,
            accession=accession,
        )
        if built:
            rows.append(built)

    return rows


def backfill_from_bulk(
    quarters: Optional[int] = None,
    cik_to_ticker: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Load historical transactions from SEC's quarterly bulk datasets."""
    quarters = quarters or SEC_BACKFILL_QUARTERS
    if cik_to_ticker is None:
        cik_to_ticker = load_cik_map(get_sp500_tickers())

    if not cik_to_ticker:
        return {"quarters": 0, "rows_added": 0, "error": "no CIK map"}

    bucket = TokenBucket(SEC_RATE_PER_SECOND, int(SEC_RATE_PER_SECOND))
    started = time.monotonic()
    total_added = 0
    loaded: List[str] = []

    for quarter in _recent_quarters(quarters):
        url = _BULK_URL.format(quarter=quarter)
        payload = _http_get_retrying(url, bucket, timeout=120)
        if not payload:
            logger.info("No bulk dataset for %s (not yet published)", quarter)
            continue

        try:
            rows = _parse_bulk_zip(payload, cik_to_ticker)
        except Exception:
            logger.exception("Could not parse the %s bulk dataset", quarter)
            continue

        added = store.upsert_trades(rows)
        total_added += added
        loaded.append(quarter)
        logger.info(
            "%s: %.1f MB, %s qualifying transactions, %s new",
            quarter, len(payload) / 1e6, len(rows), added,
        )

    elapsed = time.monotonic() - started
    logger.info(
        "Backfill complete: %s quarter(s) in %.1fs, %s new transactions",
        len(loaded), elapsed, total_added,
    )
    return {
        "quarters": loaded,
        "rows_added": total_added,
        "seconds": round(elapsed, 1),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Daily incremental
# ═══════════════════════════════════════════════════════════════════════════


def _daily_index_paths(day: date, bucket: TokenBucket) -> List[Tuple[str, str]]:
    """Form 4 filings published on one day, as ``(cik, archive path)``.

    Returns an empty list for weekends and holidays, when EDGAR publishes no
    index at all.
    """
    url = _DAILY_INDEX_URL.format(
        year=day.year,
        qtr=(day.month - 1) // 3 + 1,
        stamp=day.strftime("%Y%m%d"),
    )
    payload = _http_get_retrying(url, bucket)
    if not payload:
        return []

    entries: List[Tuple[str, str]] = []
    for line in payload.decode("latin-1").splitlines():
        # The index is fixed-width-ish: form type, company, CIK, date, path.
        if not (line.startswith("4 ") or line.startswith("4/A ")):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        path = parts[-1]
        # CIK is the field before the filing date and the path.
        try:
            cik = f"{int(parts[-3]):010d}"
        except (ValueError, IndexError):
            continue
        entries.append((cik, path))

    return entries


def _text(node: Optional[ElementTree.Element], *path: str) -> str:
    """Read a nested element's text, tolerating absent nodes."""
    current = node
    for step in path:
        if current is None:
            return ""
        current = current.find(step)
    return (current.text or "").strip() if current is not None and current.text else ""


def _parse_form4(payload: bytes, ticker_for_cik: Dict[str, str]) -> List[Dict[str, Any]]:
    """Parse one Form 4 submission into store rows.

    The archive ``.txt`` is an SGML wrapper containing the XML document; the
    XML is extracted rather than parsed as a whole, because the wrapper also
    carries headers that are not well-formed XML.
    """
    raw = payload.decode("latin-1", errors="replace")

    start = raw.find("<ownershipDocument>")
    end = raw.find("</ownershipDocument>")
    if start == -1 or end == -1:
        return []

    try:
        root = ElementTree.fromstring(raw[start:end + len("</ownershipDocument>")])
    except ElementTree.ParseError:
        return []

    issuer = root.find("issuer")
    ticker = _text(issuer, "issuerTradingSymbol").replace(".", "-").upper()
    if not ticker:
        cik = _text(issuer, "issuerCik")
        ticker = ticker_for_cik.get(f"{int(cik):010d}", "") if cik.isdigit() else ""
    if not ticker:
        return []

    is_plan = _text(root, "aff10b5One").lower() == "true"

    owner = root.find("reportingOwner")
    insider = _text(owner, "reportingOwnerId", "rptOwnerName") or "Unknown"
    relationship = owner.find("reportingOwnerRelationship") if owner is not None else None
    position = _relationship(
        {
            "title": _text(relationship, "officerTitle"),
            "is_officer": _text(relationship, "isOfficer").lower() in ("true", "1"),
            "is_director": _text(relationship, "isDirector").lower() in ("true", "1"),
            "is_ten_percent": _text(relationship, "isTenPercentOwner").lower() in ("true", "1"),
            "is_other": _text(relationship, "isOther").lower() in ("true", "1"),
        }
    )

    accession = _text(root, "periodOfReport")
    rows: List[Dict[str, Any]] = []

    table = root.find("nonDerivativeTable")
    if table is None:
        return []

    for transaction in table.findall("nonDerivativeTransaction"):
        trade_date = _to_iso(_text(transaction, "transactionDate", "value"))
        if not trade_date:
            continue

        coding = transaction.find("transactionCoding")
        amounts = transaction.find("transactionAmounts")
        ownership_node = transaction.find("ownershipNature")

        built = _build_row(
            ticker=ticker,
            insider=insider,
            position=position,
            trade_date=trade_date,
            code=_text(coding, "transactionCode"),
            shares=_number(_text(amounts, "transactionShares", "value")),
            price=_number(_text(amounts, "transactionPricePerShare", "value")),
            acquired_disposed=_text(amounts, "transactionAcquiredDisposedCode", "value") or "A",
            ownership=_text(ownership_node, "directOrIndirectOwnership", "value") or "D",
            is_plan=is_plan,
            accession=accession,
        )
        if built:
            rows.append(built)

    return rows


@dataclass
class SecFetchSummary:
    """What one incremental run did."""

    days_scanned: int = 0
    index_days_found: int = 0
    filings_seen: int = 0
    filings_fetched: int = 0
    filings_skipped: int = 0
    filings_failed: int = 0
    rows_seen: int = 0
    rows_added: int = 0
    seconds: float = 0.0
    source: str = "sec-edgar"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "days_scanned": self.days_scanned,
            "index_days_found": self.index_days_found,
            "filings_seen": self.filings_seen,
            "filings_fetched": self.filings_fetched,
            "filings_skipped": self.filings_skipped,
            "filings_failed": self.filings_failed,
            "rows_seen": self.rows_seen,
            "rows_added": self.rows_added,
            "seconds": round(self.seconds, 1),
        }


def fetch_recent(
    days: Optional[int] = None,
    cik_to_ticker: Optional[Dict[str, str]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> SecFetchSummary:
    """Fetch Form 4 filings from the last ``days`` calendar days.

    Only filings whose issuer CIK is in the covered universe are downloaded.
    That is what keeps a daily run at roughly 200 requests instead of the
    ~2,000 Form 4s the whole market files each day.
    """
    days = days or SEC_LOOKBACK_DAYS
    started = time.monotonic()
    store.init_db()

    if cik_to_ticker is None:
        cik_to_ticker = load_cik_map(get_sp500_tickers())

    summary = SecFetchSummary(days_scanned=days)
    if not cik_to_ticker:
        logger.error("No CIK map — cannot fetch from SEC")
        summary.seconds = time.monotonic() - started
        return summary

    bucket = TokenBucket(SEC_RATE_PER_SECOND, int(SEC_RATE_PER_SECOND))
    today = datetime.now(timezone.utc).date()

    # ── Collect the filings worth downloading ───────────────────────────
    wanted: List[str] = []
    seen_paths: Set[str] = set()

    for offset in range(days):
        day = today - timedelta(days=offset)
        entries = _daily_index_paths(day, bucket)
        if not entries:
            continue

        summary.index_days_found += 1
        summary.filings_seen += len(entries)

        for cik, path in entries:
            if cik in cik_to_ticker and path not in seen_paths:
                seen_paths.add(path)
                wanted.append(path)

    # Anything already downloaded on a previous run is skipped. The index
    # scan deliberately overlaps a week so late and amended filings surface,
    # and without this the overlap would mean re-fetching the same ~550
    # documents every single day.
    already_seen = len(wanted)
    wanted = store.filter_unseen_filings(wanted)
    summary.filings_skipped = already_seen - len(wanted)

    logger.info(
        "SEC: %s Form 4 filings across %s trading day(s); %s belong to covered "
        "issuers, %s already downloaded, %s to fetch",
        summary.filings_seen, summary.index_days_found, already_seen,
        summary.filings_skipped, len(wanted),
    )

    if not wanted:
        summary.seconds = time.monotonic() - started
        return summary

    # ── Download and parse them concurrently ────────────────────────────
    def _work(path: str) -> List[Dict[str, Any]]:
        payload = _http_get_retrying(_ARCHIVE_ROOT + path, bucket)
        if not payload:
            raise RuntimeError(f"could not fetch {path}")
        return _parse_form4(payload, cik_to_ticker)

    completed = 0
    fetched: List[Tuple[str, int]] = []

    with ThreadPoolExecutor(max_workers=SEC_WORKERS, thread_name_prefix="sec") as pool:
        futures = {pool.submit(_work, path): path for path in wanted}

        for future in as_completed(futures):
            path = futures[future]
            completed += 1
            try:
                rows = future.result()
            except Exception as exc:  # noqa: BLE001 — one filing must not fail the run
                summary.filings_failed += 1
                logger.debug("Form 4 fetch failed: %s", exc)
                continue

            summary.filings_fetched += 1
            if rows:
                summary.rows_seen += len(rows)
                summary.rows_added += store.upsert_trades(rows)

            # Only recorded once the rows are committed, so an interrupted
            # run re-fetches the filing rather than marking it done and
            # losing its transactions.
            fetched.append((path, len(rows)))

            if progress:
                progress(completed, len(wanted))

    store.record_filings_seen(fetched)
    store.prune_seen_filings()

    summary.seconds = time.monotonic() - started
    logger.info(
        "SEC fetch complete in %.1fs: %s filings parsed, %s failed, "
        "%s qualifying transactions, %s new",
        summary.seconds, summary.filings_fetched, summary.filings_failed,
        summary.rows_seen, summary.rows_added,
    )
    return summary


def _process_index_day(
    day: date,
    cik_to_ticker: Dict[str, str],
    bucket: TokenBucket,
) -> Tuple[int, int]:
    """Fetch and store every covered Form 4 filed on one day.

    Returns ``(filings_fetched, rows_added)``. Days on which the market was
    closed have no index and cost one request to discover.
    """
    entries = _daily_index_paths(day, bucket)
    if not entries:
        return 0, 0

    paths = [path for cik, path in entries if cik in cik_to_ticker]
    paths = store.filter_unseen_filings(paths)
    if not paths:
        return 0, 0

    def _work(path: str) -> Tuple[str, List[Dict[str, Any]]]:
        payload = _http_get_retrying(_ARCHIVE_ROOT + path, bucket)
        if not payload:
            raise RuntimeError(path)
        return path, _parse_form4(payload, cik_to_ticker)

    added = 0
    fetched: List[Tuple[str, int]] = []

    with ThreadPoolExecutor(max_workers=SEC_WORKERS, thread_name_prefix="sec-gap") as pool:
        for future in as_completed([pool.submit(_work, p) for p in paths]):
            try:
                path, rows = future.result()
            except Exception:  # noqa: BLE001 — one filing must not fail the day
                continue
            if rows:
                added += store.upsert_trades(rows)
            fetched.append((path, len(rows)))

    store.record_filings_seen(fetched)
    return len(fetched), added


def fill_history_gap(
    max_days: Optional[int] = None,
    cik_to_ticker: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Close the hole between the newest bulk dataset and the daily window.

    SEC publishes each quarterly dataset well after the quarter ends, so a
    store built from bulk plus a week of dailies has a gap of up to five
    months in the middle of it. Left alone that silently breaks the cluster
    analysis: a multi-insider purchase in the missing window simply does not
    exist as far as the report is concerned.

    The gap is closed a bounded number of days per run, oldest first, so a
    cold volume produces a usable report immediately and reaches complete
    history over the next few builds rather than blocking the first one for
    the best part of an hour. Progress is recorded per day, so an interrupted
    run resumes rather than restarting.
    """
    store.init_db()

    max_days = max_days if max_days is not None else SEC_GAP_DAYS_PER_RUN
    if max_days <= 0:
        return {"days_processed": 0, "rows_added": 0, "days_remaining": 0}

    if cik_to_ticker is None:
        cik_to_ticker = load_cik_map(get_sp500_tickers())
    if not cik_to_ticker:
        return {"days_processed": 0, "rows_added": 0, "error": "no CIK map"}

    today = datetime.now(timezone.utc).date()

    newest_quarter = _newest_published_quarter(today)
    if newest_quarter is None:
        logger.warning("No published bulk quarter found — skipping the gap fill")
        return {"days_processed": 0, "rows_added": 0, "error": "no bulk quarter"}

    # The gap starts on the first day of the quarter after the newest one that
    # is actually published.
    year, quarter = int(newest_quarter[:4]), int(newest_quarter[-1])
    gap_start = (
        date(year + 1, 1, 1) if quarter == 4 else date(year, quarter * 3 + 1, 1)
    )

    processed = store.processed_index_days()
    pending = [
        gap_start + timedelta(days=offset)
        for offset in range((today - gap_start).days + 1)
        if (gap_start + timedelta(days=offset)).strftime("%Y-%m-%d") not in processed
        # Weekends have no filings and no index; skipping them saves a
        # request each and roughly a third of the calendar.
        and (gap_start + timedelta(days=offset)).weekday() < 5
    ]

    if not pending:
        logger.info("History gap is fully closed")
        return {"days_processed": 0, "rows_added": 0, "days_remaining": 0}

    batch = pending[:max_days]
    logger.info(
        "Filling history gap: %s trading day(s) outstanding from %s, processing %s now",
        len(pending), gap_start, len(batch),
    )

    bucket = TokenBucket(SEC_RATE_PER_SECOND, int(SEC_RATE_PER_SECOND))
    started = time.monotonic()
    total_added = 0
    total_filings = 0

    for day in batch:
        try:
            filings, added = _process_index_day(day, cik_to_ticker, bucket)
        except Exception:
            logger.exception("Gap fill failed for %s", day)
            continue

        total_filings += filings
        total_added += added
        store.record_index_day(day.strftime("%Y-%m-%d"), filings, added)

    elapsed = time.monotonic() - started
    remaining = len(pending) - len(batch)
    logger.info(
        "Gap fill: %s day(s) in %.0fs, %s filings, %s new transactions, %s day(s) remaining",
        len(batch), elapsed, total_filings, total_added, remaining,
    )
    return {
        "days_processed": len(batch),
        "filings": total_filings,
        "rows_added": total_added,
        "days_remaining": remaining,
        "seconds": round(elapsed, 1),
    }


def refresh(full: bool = False) -> Dict[str, Any]:
    """Bring the trade store up to date from SEC EDGAR.

    A cold store is backfilled from the bulk quarterly datasets first, then
    caught up to today from the daily indexes. A warm store only does the
    second step.
    """
    store.init_db()
    tickers = get_sp500_tickers()
    cik_map = load_cik_map(tickers)

    result: Dict[str, Any] = {"universe": len(tickers), "cik_mapped": len(cik_map)}

    if full or store.is_empty():
        logger.info("Backfilling from SEC bulk datasets")
        result["backfill"] = backfill_from_bulk(cik_to_ticker=cik_map)

    # The recent window first, so today's report is right even if the gap
    # fill below is interrupted.
    summary = fetch_recent(cik_to_ticker=cik_map)
    result.update(summary.as_dict())

    result["gap_fill"] = fill_history_gap(cik_to_ticker=cik_map)
    result["coverage"] = store.index_coverage()
    return result
