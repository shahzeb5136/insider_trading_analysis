"""Insider transactions from SEC EDGAR — the authoritative source.

Three paths, because EDGAR publishes the same data in different shapes and
each is right for a different job:

    cold store  ──►  quarterly bulk ZIPs        ~10MB each, parses in ~1s
                     (SUBMISSION + REPORTINGOWNER + NONDERIV_TRANS)

    the gap     ──►  daily filing indexes       bulk is published a quarter in
                     worked oldest-first,        arrears, so there is a hole of
                     a bounded chunk per run     up to five months before today

    daily run   ──►  daily filing index         1 request per calendar day
                     └─► Form 4 XML for covered issuers only   ~150 requests

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
whether a trade was made under a pre-arranged 10b5-1 plan, which is exactly
the distinction between a scheduled disposal and a decision.

Identity of a transaction
-------------------------
A row's key is built from the filing's accession number plus the transaction's
content — date, code, shares, price, direction, ownership — plus its
occurrence index among identical lines in the same filing. Three things that
design has to survive, each of which an earlier version got wrong:

* The bulk TSV carries prices to 2 decimals and the Form 4 XML to 4, so the
  same trade arrived with two different dollar values. Prices are rounded to
  cents before anything is derived from them.
* One filing routinely reports several identical lots (separate executions,
  or the same holding through two trusts). The occurrence index keeps them
  apart without depending on the order the two sources list them in.
* The 10b5-1 flag was once part of the descriptive text, so correcting the
  flag changed the key. It now lives only in its own column.

Rate limiting follows SEC's published policy: a declared User-Agent carrying
a real contact address, and no more than 10 requests a second. This runs at 8.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
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
from ratelimit import TokenBucket
from universe import get_sp500_tickers

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
    "A": "Grant/Award",            # Grant, award or other acquisition FROM the issuer
    # D is the mirror of A — a disposition back TO the issuer, typically a
    # buyback or a share surrender. It is a disposal, not compensation, and
    # bucketing it with grants would show a nine-figure buyback as an award.
    # It is kept out of "Sell" too: the issuer is the counterparty, so it says
    # nothing about what the open market would pay.
    "D": "Disposition to Issuer",
    "F": "Tax Withholding",        # Shares withheld to pay tax or exercise price
    "M": "Exercise/Conversion",    # Exercise or conversion of a derivative
    "C": "Exercise/Conversion",    # Conversion of a derivative security
    "X": "Exercise/Conversion",    # Exercise of an in-the-money derivative
    "O": "Exercise/Conversion",    # Exercise of an out-of-the-money derivative
    "G": "Gift",                   # Bona fide gift
    "V": "Other",                  # Transaction voluntarily reported early
    "J": "Other",                  # Other acquisition or disposition
    "K": "Other",                  # Equity swap or similar
    "U": "Other",                  # Disposition in a tender of shares
    "W": "Other",                  # Acquisition or disposition by will or inheritance
    "H": "Other",
    "I": "Other",
    "L": "Other",
    "E": "Other",
}

# Form 4 booleans arrive as either true/false or 1/0 depending on the filing
# agent. In the 2026q1 bulk file the 10b5-1 flag was "1" for 3,620 filings and
# "true" for only 1,162 — a test against "true" alone missed three quarters
# of them.
_TRUE_VALUES = frozenset({"true", "1", "y", "yes"})


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUE_VALUES


class SecUnavailable(Exception):
    """A request failed for a reason other than the object not existing.

    Raised only after the retry budget is exhausted. Callers that mark
    progress — the gap fill in particular — must treat this differently from
    a 404: a missing daily index means the market was closed, but a failed
    one means nothing is known about that day yet.
    """


# ═══════════════════════════════════════════════════════════════════════════
# HTTP
# ═══════════════════════════════════════════════════════════════════════════


def _http_get(url: str, timeout: int = 45, method: str = "GET") -> bytes:
    """One request with SEC's required User-Agent."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": SEC_USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
            "Host": "www.sec.gov",
        },
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            payload = gzip.decompress(payload)
        return payload


def _http_get_retrying(
    url: str, bucket: Optional[TokenBucket] = None, timeout: int = 45
) -> Optional[bytes]:
    """GET with the rate limit applied and a bounded retry.

    Returns None only when the object definitively does not exist — a 404,
    or the 403 EDGAR answers for an index file that has not been published
    yet. Every other failure raises ``SecUnavailable`` once the retries are
    spent, so a caller can tell "there is nothing there" from "I could not
    find out". The distinction is what stops the gap fill from marking a day
    as done because the request happened to time out.
    """
    last_error: Optional[BaseException] = None

    for attempt in range(1, SEC_MAX_RETRIES + 1):
        if bucket:
            bucket.take()
        try:
            return _http_get(url, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                return None
            last_error = exc
            if exc.code in (429, 500, 502, 503, 504) and attempt < SEC_MAX_RETRIES:
                wait = 2.0 * attempt
                logger.warning("SEC returned %s for %s — backing off %.0fs", exc.code, url, wait)
                time.sleep(wait)
                continue
            break
        except Exception as exc:  # noqa: BLE001 — transport errors of every kind retry
            last_error = exc
            if attempt < SEC_MAX_RETRIES:
                time.sleep(1.5 * attempt)
                continue

    raise SecUnavailable(f"{url}: {last_error}")


# ═══════════════════════════════════════════════════════════════════════════
# Ticker ↔ CIK
# ═══════════════════════════════════════════════════════════════════════════


def _pad_cik(value: Any) -> Optional[str]:
    """CIKs arrive as ints, bare strings and zero-padded strings. One form."""
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    return f"{int(text):010d}"


def load_cik_map(tickers: Sequence[str]) -> Dict[str, str]:
    """Map the covered universe to issuer CIKs.

    Keyed by CIK because that is what both the daily index and the bulk
    SUBMISSION table identify an issuer by. The ticker value is only a
    fallback: when a filing carries its own trading symbol, that wins.

    One CIK can carry several tickers — Alphabet is GOOG and GOOGL, Fox is
    FOX and FOXA — and an earlier version collapsed them to one and then
    matched filings by ticker string, which dropped every Alphabet filing
    ($422M in one quarter) because they all say GOOGL. Membership is now
    tested on the CIK, so a dual-class issuer is in or out as a whole.
    """
    try:
        payload = _http_get_retrying(_COMPANY_TICKERS_URL)
    except SecUnavailable as exc:
        logger.error("Could not load SEC company_tickers.json: %s", exc)
        return {}
    if not payload:
        logger.error("SEC company_tickers.json is missing")
        return {}

    try:
        raw = json.loads(payload)
    except json.JSONDecodeError:
        logger.exception("company_tickers.json did not parse")
        return {}

    # EDGAR spells class shares with a hyphen (BRK-B); the constituent lists
    # use the same convention, but normalise both to be safe.
    wanted = {t.replace(".", "-").upper() for t in tickers}

    by_cik: Dict[str, List[str]] = defaultdict(list)
    for entry in raw.values():
        symbol = str(entry.get("ticker", "")).replace(".", "-").upper()
        cik = _pad_cik(entry.get("cik_str"))
        if symbol in wanted and cik:
            by_cik[cik].append(symbol)

    # A deterministic representative per CIK. Shortest first, so GOOG stands
    # for Alphabet when a filing carries no symbol of its own.
    mapping = {cik: sorted(symbols, key=lambda s: (len(s), s))[0] for cik, symbols in by_cik.items()}

    covered = {s for symbols in by_cik.values() for s in symbols}
    missing = sorted(wanted - covered)
    if missing:
        logger.info(
            "%s ticker(s) have no CIK in company_tickers.json and cannot be "
            "covered: %s", len(missing), ", ".join(missing[:12]),
        )

    logger.info(
        "Mapped %s tickers to %s issuer CIKs", len(covered), len(mapping),
    )
    return mapping


# ═══════════════════════════════════════════════════════════════════════════
# Row construction
# ═══════════════════════════════════════════════════════════════════════════


def _describe(code: str, price: float) -> str:
    """A human-readable description of the transaction.

    Deliberately carries only the code and the price. The 10b5-1 flag used to
    be appended here, which meant correcting the flag changed the row's
    identity; it now lives solely in the ``is_plan`` column.
    """
    label = {
        "P": "Purchase", "S": "Sale", "A": "Grant or award",
        "D": "Disposition to issuer", "F": "Shares withheld for tax",
        "M": "Exercise of derivative security",
        "C": "Conversion of derivative security",
        "X": "Exercise of in-the-money derivative",
        "O": "Exercise of out-of-the-money derivative",
        "G": "Bona fide gift",
    }.get(code, f"Transaction (code {code})")

    if price > 0:
        return f"{label} at price {price:,.2f} per share."
    return f"{label}."


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


def _content_tuple(
    trade_date: str, code: str, shares: float, price: float,
    acquired_disposed: str, ownership: str,
) -> Tuple[str, str, str, str, str, str]:
    """What makes two lines in one filing "the same transaction"."""
    return (
        trade_date,
        str(code).upper(),
        f"{float(shares):.2f}",
        f"{float(price):.2f}",
        str(acquired_disposed).upper()[:1],
        str(ownership).upper()[:1],
    )


def _occurrence_indexes(contents: Sequence[Tuple]) -> List[int]:
    """For each line, how many identical lines precede it in the filing.

    Both ingestion paths produce the same multiset of lines for a filing, so
    numbering identical lines 0, 1, 2… gives the same keys regardless of the
    order the TSV and the XML happen to list them in.
    """
    seen: Counter = Counter()
    out: List[int] = []
    for content in contents:
        out.append(seen[content])
        seen[content] += 1
    return out


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
    occurrence: int,
) -> Optional[Dict[str, Any]]:
    """Turn one parsed SEC transaction into a store row.

    Returns None for anything below the reporting threshold, which is most
    of them — the bulk feed carries every Form 4 filed by every issuer.
    """
    if not shares or price is None:
        return None

    # Cents. The bulk TSV already carries two decimals; the XML can carry
    # four, and multiplied by a six-figure share count that difference was
    # enough to give the same trade two different identities.
    price = round(float(price), 2)
    shares = float(shares)
    value = shares * price
    if abs(value) < MIN_TRADE_VALUE:
        return None

    # SEC signs by direction: A(cquired) adds to the holding, D(isposed)
    # reduces it. Store the value signed the same way, so a sale is negative.
    direction = str(acquired_disposed).upper()[:1] or "A"
    if direction == "D":
        value = -value

    code = str(code).upper()[:2]
    ownership = str(ownership).upper()[:1] or "D"
    text = _describe(code, price)

    return {
        "trade_key": store.trade_key(
            ticker, insider, trade_date, shares, value, text,
            accession=accession, ownership=ownership, occurrence=occurrence,
        ),
        "ticker": ticker.upper(),
        "insider": insider.strip(),
        "position": position.strip(),
        "trade_date": trade_date,
        "trade_type": _TRANS_CODES.get(code, "Other"),
        "text": text,
        "transaction_desc": f"{code} / {accession}",
        "shares": shares,
        "price": price,
        "value": value,
        "abs_value": abs(value),
        "ownership": ownership,
        "trans_code": code,
        "is_plan": 1 if is_plan else 0,
        "source": "sec",
        "first_seen": datetime.now(timezone.utc).isoformat(),
    }


# Form 4s are self-reported and filers do mistype dates. A handful of rows in
# every backfill carry years like 0024 (for 2024) or dates a year or two in
# the future — 4 in 28,000 on the last full load — and they are pure poison
# for a report that sorts by date: a 2028 transaction sits at the top of
# "most recent" permanently, and an 0024 one silently becomes the start of
# the reported history.
_EARLIEST_PLAUSIBLE = date(1990, 1, 1)


def _to_iso(value: str) -> Optional[str]:
    """Parse an SEC date to ``YYYY-MM-DD``, rejecting implausible ones.

    SEC bulk dates are ``31-MAR-2026``; Form 4 XML dates are already ISO.
    Anything before 1990 or more than two days ahead of now is treated as a
    filing error — two days rather than zero because filings arrive from
    every US timezone and a same-day transaction can read as tomorrow in UTC.
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
    """The last ``count`` completed quarters, newest first."""
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
    months too late and silently leave that quarter missing from the store.
    One or two HEAD requests settle it for certain.
    """
    for quarter in _recent_quarters(6, today):
        url = _BULK_URL.format(quarter=quarter)
        try:
            _http_get(url, timeout=30, method="HEAD")
            return quarter
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                continue
            logger.warning("Probing %s returned HTTP %s", quarter, exc.code)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not probe %s: %s", quarter, exc)
            return None
    return None


def _parse_bulk_zip(payload: bytes, cik_to_ticker: Dict[str, str]) -> List[Dict[str, Any]]:
    """Extract qualifying transactions from one quarterly dataset.

    The three tables join on ACCESSION_NUMBER: SUBMISSION carries the issuer
    and the 10b5-1 flag, REPORTINGOWNER the filer, NONDERIV_TRANS the trades.
    Issuers are matched on CIK, never on the symbol string.
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

    # accession -> (ticker, is_plan)
    issuers: Dict[str, Tuple[str, bool]] = {}
    for row in submissions:
        cik = _pad_cik(row.get("ISSUERCIK"))
        if cik not in cik_to_ticker:
            continue
        symbol = (row.get("ISSUERTRADINGSYMBOL") or "").strip().replace(".", "-").upper()
        # The filing's own symbol wins: for a dual-class issuer it says which
        # class actually traded. The map is only the fallback.
        ticker = symbol.split()[0] if symbol else cik_to_ticker[cik]
        issuers[row["ACCESSION_NUMBER"]] = (ticker, _truthy(row.get("AFF10B5ONE")))

    # accession -> (name, relationship). Joint filings list several owners;
    # the first is taken, on both ingestion paths, so they agree.
    filers: Dict[str, Tuple[str, str]] = {}
    for row in owners:
        accession = row["ACCESSION_NUMBER"]
        if accession not in issuers or accession in filers:
            continue
        relationship = (row.get("RPTOWNER_TITLE") or "").strip()
        if not relationship:
            relationship = (row.get("RPTOWNER_RELATIONSHIP") or "").strip() or "Insider"
        filers[accession] = ((row.get("RPTOWNERNAME") or "").strip(), relationship)

    # Group the transaction lines per filing so identical lines can be numbered.
    per_filing: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in transactions:
        accession = row["ACCESSION_NUMBER"]
        if accession not in issuers:
            continue
        trade_date = _to_iso(row.get("TRANS_DATE", ""))
        shares = _number(row.get("TRANS_SHARES"))
        price = _number(row.get("TRANS_PRICEPERSHARE"))
        if not trade_date or not shares or price is None:
            continue
        per_filing[accession].append(
            {
                "trade_date": trade_date,
                "code": row.get("TRANS_CODE", ""),
                "shares": shares,
                "price": price,
                "acquired_disposed": row.get("TRANS_ACQUIRED_DISP_CD", "A"),
                "ownership": row.get("DIRECT_INDIRECT_OWNERSHIP", "D"),
            }
        )

    rows: List[Dict[str, Any]] = []
    for accession, lines in per_filing.items():
        ticker, is_plan = issuers[accession]
        insider, position = filers.get(accession, ("Unknown", "Insider"))
        contents = [
            _content_tuple(
                l["trade_date"], l["code"], l["shares"], l["price"],
                l["acquired_disposed"], l["ownership"],
            )
            for l in lines
        ]
        for line, occurrence in zip(lines, _occurrence_indexes(contents)):
            built = _build_row(
                ticker=ticker, insider=insider, position=position,
                trade_date=line["trade_date"], code=line["code"],
                shares=line["shares"], price=line["price"],
                acquired_disposed=line["acquired_disposed"],
                ownership=line["ownership"], is_plan=is_plan,
                accession=accession, occurrence=occurrence,
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
        return {"quarters": [], "rows_added": 0, "error": "no CIK map"}

    bucket = TokenBucket(SEC_RATE_PER_SECOND, int(SEC_RATE_PER_SECOND))
    started = time.monotonic()
    total_added = 0
    loaded: List[str] = []
    failed: List[str] = []

    for quarter in _recent_quarters(quarters):
        url = _BULK_URL.format(quarter=quarter)
        try:
            payload = _http_get_retrying(url, bucket, timeout=120)
        except SecUnavailable as exc:
            logger.warning("Bulk dataset %s could not be fetched: %s", quarter, exc)
            failed.append(quarter)
            continue
        if not payload:
            logger.info("No bulk dataset for %s (not yet published)", quarter)
            continue

        try:
            rows = _parse_bulk_zip(payload, cik_to_ticker)
        except Exception:
            logger.exception("Could not parse the %s bulk dataset", quarter)
            failed.append(quarter)
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
        "Backfill complete: %s quarter(s) in %.1fs, %s new transactions%s",
        len(loaded), elapsed, total_added,
        f", {len(failed)} could not be fetched: {failed}" if failed else "",
    )
    return {
        "quarters": loaded,
        "failed_quarters": failed,
        "rows_added": total_added,
        "seconds": round(elapsed, 1),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Daily filings
# ═══════════════════════════════════════════════════════════════════════════


def _daily_index_paths(day: date, bucket: TokenBucket) -> Optional[List[Tuple[str, str]]]:
    """Form 4 filings published on one day, as ``(cik, archive path)``.

    Returns an empty list for weekends and holidays, when EDGAR publishes no
    index at all — and None when the index could not be retrieved, which is
    a different thing and must not be recorded as "nothing filed".
    """
    url = _DAILY_INDEX_URL.format(
        year=day.year,
        qtr=(day.month - 1) // 3 + 1,
        stamp=day.strftime("%Y%m%d"),
    )
    try:
        payload = _http_get_retrying(url, bucket)
    except SecUnavailable as exc:
        logger.warning("Daily index for %s unavailable: %s", day, exc)
        return None
    if not payload:
        return []

    entries: List[Tuple[str, str]] = []
    for line in payload.decode("latin-1").splitlines():
        # form type, company name, CIK, date filed, path — whitespace-padded
        # columns. The company name can itself contain spaces and digits, so
        # the fixed fields are read from the right-hand end.
        if not (line.startswith("4 ") or line.startswith("4/A ")):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        cik = _pad_cik(parts[-3])
        if not cik:
            continue
        entries.append((cik, parts[-1]))
    return entries


def _accession_from_path(path: str) -> str:
    """``edgar/data/1770787/0001610717-26-000396.txt`` → ``0001610717-26-000396``."""
    name = path.rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".txt") else name


def _text(node: Optional[ElementTree.Element], *path: str) -> str:
    """Read a nested element's text, tolerating absent nodes."""
    current = node
    for step in path:
        if current is None:
            return ""
        current = current.find(step)
    return (current.text or "").strip() if current is not None and current.text else ""


def _parse_form4(
    payload: bytes, ticker_for_cik: Dict[str, str], accession: str
) -> List[Dict[str, Any]]:
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
    ticker = ticker.split()[0] if ticker else ""
    if not ticker:
        cik = _pad_cik(_text(issuer, "issuerCik"))
        ticker = ticker_for_cik.get(cik or "", "")
    if not ticker:
        return []

    is_plan = _truthy(_text(root, "aff10b5One"))

    owner = root.find("reportingOwner")
    insider = _text(owner, "reportingOwnerId", "rptOwnerName") or "Unknown"
    relationship = owner.find("reportingOwnerRelationship") if owner is not None else None
    position = _relationship(
        {
            "title": _text(relationship, "officerTitle"),
            "is_officer": _truthy(_text(relationship, "isOfficer")),
            "is_director": _truthy(_text(relationship, "isDirector")),
            "is_ten_percent": _truthy(_text(relationship, "isTenPercentOwner")),
            "is_other": _truthy(_text(relationship, "isOther")),
        }
    )

    table = root.find("nonDerivativeTable")
    if table is None:
        return []

    lines: List[Dict[str, Any]] = []
    for transaction in table.findall("nonDerivativeTransaction"):
        trade_date = _to_iso(_text(transaction, "transactionDate", "value"))
        coding = transaction.find("transactionCoding")
        amounts = transaction.find("transactionAmounts")
        nature = transaction.find("ownershipNature")
        shares = _number(_text(amounts, "transactionShares", "value"))
        price = _number(_text(amounts, "transactionPricePerShare", "value"))
        if not trade_date or not shares or price is None:
            continue
        lines.append(
            {
                "trade_date": trade_date,
                "code": _text(coding, "transactionCode"),
                "shares": shares,
                "price": price,
                "acquired_disposed": _text(amounts, "transactionAcquiredDisposedCode", "value") or "A",
                "ownership": _text(nature, "directOrIndirectOwnership", "value") or "D",
            }
        )

    contents = [
        _content_tuple(
            l["trade_date"], l["code"], l["shares"], l["price"],
            l["acquired_disposed"], l["ownership"],
        )
        for l in lines
    ]

    rows: List[Dict[str, Any]] = []
    for line, occurrence in zip(lines, _occurrence_indexes(contents)):
        built = _build_row(
            ticker=ticker, insider=insider, position=position,
            trade_date=line["trade_date"], code=line["code"],
            shares=line["shares"], price=line["price"],
            acquired_disposed=line["acquired_disposed"],
            ownership=line["ownership"], is_plan=is_plan,
            accession=accession, occurrence=occurrence,
        )
        if built:
            rows.append(built)
    return rows


def _fetch_filings(
    paths: Sequence[str],
    cik_to_ticker: Dict[str, str],
    bucket: TokenBucket,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Tuple[int, int, int, int]:
    """Download and parse filings concurrently, storing what qualifies.

    Returns ``(fetched, failed, rows_seen, rows_added)``. A filing is marked
    seen only after its rows are committed, so an interrupted run re-fetches
    it rather than marking it done and losing its transactions. A filing
    EDGAR no longer has (404) is recorded as seen with zero rows, since
    retrying it every day would never change the answer.
    """

    def _work(path: str) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
        payload = _http_get_retrying(_ARCHIVE_ROOT + path, bucket)
        if payload is None:
            return path, None
        return path, _parse_form4(payload, cik_to_ticker, _accession_from_path(path))

    fetched = failed = rows_seen = rows_added = 0
    completed = 0
    seen: List[Tuple[str, int]] = []

    with ThreadPoolExecutor(max_workers=SEC_WORKERS, thread_name_prefix="sec") as pool:
        futures = {pool.submit(_work, p): p for p in paths}
        for future in as_completed(futures):
            completed += 1
            try:
                path, rows = future.result()
            except Exception as exc:  # noqa: BLE001 — one filing must not fail the run
                failed += 1
                logger.debug("Form 4 fetch failed for %s: %s", futures[future], exc)
                continue

            fetched += 1
            if rows:
                rows_seen += len(rows)
                rows_added += store.upsert_trades(rows)
            seen.append((path, len(rows or [])))

            if progress:
                progress(completed, len(paths))

    store.record_filings_seen(seen)
    return fetched, failed, rows_seen, rows_added


@dataclass
class SecFetchSummary:
    """What one incremental run did."""

    days_scanned: int = 0
    index_days_found: int = 0
    index_days_unavailable: int = 0
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
            "index_days_unavailable": self.index_days_unavailable,
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
    That is what keeps a daily run at roughly 150 requests instead of the
    ~2,000 Form 4s the whole market files each day. Filings already
    downloaded on a previous run are skipped, so the week-long overlap that
    catches late and amended filings costs only the index requests.
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

    wanted: List[str] = []
    for offset in range(days):
        day = today - timedelta(days=offset)
        entries = _daily_index_paths(day, bucket)
        if entries is None:
            summary.index_days_unavailable += 1
            continue
        if not entries:
            continue
        summary.index_days_found += 1
        summary.filings_seen += len(entries)
        wanted.extend(path for cik, path in entries if cik in cik_to_ticker)

    wanted = list(dict.fromkeys(wanted))  # dedupe, keep order
    before = len(wanted)
    wanted = store.filter_unseen_filings(wanted)
    summary.filings_skipped = before - len(wanted)

    logger.info(
        "SEC: %s Form 4 filings across %s trading day(s); %s belong to covered "
        "issuers, %s already downloaded, %s to fetch%s",
        summary.filings_seen, summary.index_days_found, before,
        summary.filings_skipped, len(wanted),
        f" ({summary.index_days_unavailable} index day(s) unavailable)"
        if summary.index_days_unavailable else "",
    )

    if wanted:
        fetched, failed, rows_seen, rows_added = _fetch_filings(
            wanted, cik_to_ticker, bucket, progress
        )
        summary.filings_fetched = fetched
        summary.filings_failed = failed
        summary.rows_seen = rows_seen
        summary.rows_added = rows_added
        store.prune_seen_filings()

    summary.seconds = time.monotonic() - started
    logger.info(
        "SEC fetch complete in %.1fs: %s filings parsed, %s failed, "
        "%s qualifying transactions, %s new",
        summary.seconds, summary.filings_fetched, summary.filings_failed,
        summary.rows_seen, summary.rows_added,
    )
    return summary


# ═══════════════════════════════════════════════════════════════════════════
# History gap
# ═══════════════════════════════════════════════════════════════════════════


def _process_index_day(
    day: date,
    cik_to_ticker: Dict[str, str],
    bucket: TokenBucket,
) -> Optional[Tuple[int, int]]:
    """Fetch and store every covered Form 4 filed on one day.

    Returns ``(filings_fetched, rows_added)``, or None when the day's index
    could not be retrieved — in which case the caller must not record the
    day as done.
    """
    entries = _daily_index_paths(day, bucket)
    if entries is None:
        return None
    if not entries:
        return 0, 0

    paths = [path for cik, path in entries if cik in cik_to_ticker]
    paths = store.filter_unseen_filings(paths)
    if not paths:
        return 0, 0

    fetched, failed, _, added = _fetch_filings(paths, cik_to_ticker, bucket)
    if failed and fetched == 0:
        # Nothing at all came back: the day is not done, whatever the index said.
        return None
    return fetched, added


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
    run resumes rather than restarting — and a day whose index could not be
    fetched is left unrecorded, so it is retried rather than lost.
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
    total_added = total_filings = 0
    unavailable = 0

    for day in batch:
        try:
            result = _process_index_day(day, cik_to_ticker, bucket)
        except Exception:
            logger.exception("Gap fill failed for %s", day)
            unavailable += 1
            continue
        if result is None:
            unavailable += 1
            continue
        filings, added = result
        total_filings += filings
        total_added += added
        store.record_index_day(day.strftime("%Y-%m-%d"), filings, added)

    elapsed = time.monotonic() - started
    remaining = len(pending) - len(batch) + unavailable
    logger.info(
        "Gap fill: %s day(s) in %.0fs, %s filings, %s new transactions, "
        "%s day(s) remaining%s",
        len(batch) - unavailable, elapsed, total_filings, total_added, remaining,
        f" ({unavailable} left for retry)" if unavailable else "",
    )
    return {
        "days_processed": len(batch) - unavailable,
        "days_unavailable": unavailable,
        "filings": total_filings,
        "rows_added": total_added,
        "days_remaining": remaining,
        "seconds": round(elapsed, 1),
    }


def refresh(full: bool = False) -> Dict[str, Any]:
    """Bring the trade store up to date from SEC EDGAR.

    A cold store is backfilled from the bulk quarterly datasets first, then
    caught up to today from the daily indexes, then a slice of the gap
    between the two is closed. A warm store only does the last two.
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
