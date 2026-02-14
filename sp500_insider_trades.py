"""
S&P 500 Insider Trades Scraper ($1M+ transactions only)
========================================================
Uses yfinance (free, no API key needed) to pull insider transactions
for all S&P 500 companies, filtering for trades >= $1,000,000.

Rate-limiting strategy:
- Random delay of 2-5s between tickers (~360 req/hr Yahoo unofficial limit)
- Exponential backoff with jitter on 429 errors (up to 3 retries)
- Checkpoint saves every 25 tickers so you never lose progress
- Resume support: re-run the script and it picks up where it left off
- Batch fetching: yfinance pulls all holder data in 1 API call per ticker,
  so we only make ~500 total requests (well within daily limits)

Estimated runtime: ~25-45 minutes for all ~500 tickers

Output: CSV with all insider trades >= $1M

Usage:
  pip install yfinance pandas lxml html5lib
  python sp500_insider_trades.py
"""

import yfinance as yf
import pandas as pd
import time
import random
import os
import json
import sys
import argparse
import urllib.request
from datetime import datetime, date

# ─── CONFIGURATION ───────────────────────────────────────────────────────────
MIN_TRADE_VALUE = 1_000_000       # $1M minimum trade value
DELAY_MIN = 2.0                   # Min seconds between requests
DELAY_MAX = 5.0                   # Max seconds between requests
MAX_RETRIES = 3                   # Retries per ticker on rate-limit
CHECKPOINT_EVERY = 25             # Save progress every N tickers
OUTPUT_FILE = "insider_trades_1m_plus.csv"
CHECKPOINT_FILE = ".checkpoint_progress.json"
FAILED_LOG = "failed_tickers.txt"


# ═══════════════════════════════════════════════════════════════════════════════
# S&P 500 TICKER FETCHING — 3 methods with automatic fallback
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_from_wikipedia() -> list[str] | None:
    """Method 1: Scrape Wikipedia with proper headers to avoid 403."""
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8")

        tables = pd.read_html(html)
        tickers = (
            tables[0]["Symbol"]
            .str.strip()
            .str.replace(".", "-", regex=False)  # BRK.B -> BRK-B
            .tolist()
        )
        return tickers if len(tickers) > 400 else None
    except Exception as e:
        print(f"(failed: {e})")
        return None


def _fetch_from_github() -> list[str] | None:
    """Method 2: datahub/datasets GitHub repo (updated weekly via CI)."""
    try:
        url = (
            "https://raw.githubusercontent.com/datasets/"
            "s-and-p-500-companies/main/data/constituents.csv"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Python/3"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            csv_text = resp.read().decode("utf-8")

        from io import StringIO
        df = pd.read_csv(StringIO(csv_text))
        tickers = (
            df["Symbol"]
            .str.strip()
            .str.replace(".", "-", regex=False)
            .tolist()
        )
        return tickers if len(tickers) > 400 else None
    except Exception as e:
        print(f"(failed: {e})")
        return None


def _hardcoded_sp500() -> list[str]:
    """Method 3: Hardcoded S&P 500 tickers (as of early 2026).
    Note: Some tickers may have been added/removed since this list was made.
    The list is ~490 tickers; a few that got delisted are excluded.
    """
    return [
        "AAPL","ABBV","ABT","ACN","ADBE","ADI","ADM","ADP","ADSK","AEE",
        "AEP","AES","AFL","AIG","AIZ","AJG","AKAM","ALB","ALGN","ALL",
        "ALLE","AMAT","AMCR","AMD","AME","AMGN","AMP","AMT","AMZN","ANET",
        "ANSS","AON","AOS","APA","APD","APH","APTV","ARE","ATO","AVGO",
        "AVB","AVY","AWK","AXP","AZO","BA","BAC","BAX","BBWI","BBY",
        "BDX","BEN","BF-B","BG","BIIB","BIO","BK","BKNG","BKR","BLK",
        "BMY","BR","BRK-B","BRO","BSX","BWA","BXP","C","CAG","CAH",
        "CARR","CAT","CB","CBOE","CBRE","CCI","CCL","CDAY","CDNS","CDW",
        "CE","CEG","CF","CFG","CHD","CHRW","CHTR","CI","CINF","CL",
        "CLX","CMA","CMCSA","CME","CMG","CMI","CMS","CNC","CNP","COF",
        "COO","COP","COST","CPB","CPRT","CPT","CRL","CRM","CSCO","CSGP",
        "CSX","CTAS","CTRA","CTSH","CTVA","CVS","CVX","CZR","D","DAL",
        "DD","DE","DECK","DFS","DG","DGX","DHI","DHR","DIS","DLR",
        "DLTR","DOV","DOW","DPZ","DRI","DTE","DUK","DVA","DVN","DXCM",
        "EA","EBAY","ECL","ED","EFX","EIX","EL","EMN","EMR","ENPH",
        "EOG","EPAM","EQIX","EQR","EQT","ES","ESS","ETN","ETR","EVRG",
        "EW","EXC","EXPD","EXPE","EXR","F","FANG","FAST","FBHS","FCX",
        "FDS","FDX","FE","FFIV","FIS","FISV","FITB","FMC","FOX","FOXA",
        "FRT","FSLR","FTNT","FTV","GD","GE","GEHC","GEN","GILD","GIS",
        "GL","GLW","GM","GNRC","GOOG","GOOGL","GPC","GPN","GRMN","GS",
        "GWW","HAL","HAS","HBAN","HCA","HD","HOLX","HON","HPE","HPQ",
        "HRL","HSIC","HST","HSY","HUBB","HUM","HWM","IBM","ICE","IDXX",
        "IEX","IFF","ILMN","INCY","INTC","INTU","INVH","IP","IPG","IQV",
        "IR","IRM","ISRG","IT","ITW","IVZ","J","JBHT","JCI","JKHY",
        "JNJ","JNPR","JPM","K","KDP","KEY","KEYS","KHC","KIM","KLAC",
        "KMB","KMI","KMX","KO","KR","KVUE","L","LDOS","LEN","LH",
        "LHX","LIN","LKQ","LLY","LMT","LNT","LOW","LRCX","LULU","LUV",
        "LVS","LW","LYB","LYV","MA","MAA","MAR","MAS","MCD","MCHP",
        "MCK","MCO","MDLZ","MDT","MET","META","MGM","MHK","MKC","MKTX",
        "MLM","MMC","MMM","MNST","MO","MOH","MOS","MPC","MPWR","MRK",
        "MRNA","MRO","MS","MSCI","MSFT","MSI","MTB","MTCH","MTD","MU",
        "NCLH","NDAQ","NDSN","NEE","NEM","NFLX","NI","NKE","NOC","NOW",
        "NRG","NSC","NTAP","NTRS","NUE","NVDA","NVR","NWS","NWSA","NXPI",
        "O","ODFL","OGN","OKE","OMC","ON","ORCL","ORLY","OTIS","OXY",
        "PARA","PAYC","PAYX","PCAR","PCG","PEG","PEP","PFE","PFG","PG",
        "PGR","PH","PHM","PKG","PLD","PM","PNC","PNR","PNW","POOL",
        "PPG","PPL","PRU","PSA","PSX","PTC","PVH","PWR","PYPL","QCOM",
        "QRVO","RCL","RE","REG","REGN","RF","RHI","RJF","RL","RMD",
        "ROK","ROL","ROP","ROST","RSG","RTX","RVTY","SBAC","SBUX","SCHW",
        "SEE","SHW","SJM","SLB","SMCI","SNA","SNPS","SO","SPG","SPGI",
        "SRE","STE","STLD","STT","STX","STZ","SWK","SWKS","SYF","SYK",
        "SYY","T","TAP","TDG","TDY","TECH","TEL","TER","TFC","TFX",
        "TGT","TMO","TMUS","TPR","TRGP","TRMB","TROW","TRV","TSCO","TSLA",
        "TSN","TT","TTWO","TXN","TXT","TYL","UAL","UDR","UHS","ULTA",
        "UNH","UNP","UPS","URI","USB","V","VICI","VLO","VMC","VRSK",
        "VRSN","VRTX","VTR","VTRS","VZ","WAB","WAT","WBA","WBD","WDC",
        "WEC","WELL","WFC","WHR","WM","WMB","WMT","WRB","WRK","WST",
        "WTW","WY","WYNN","XEL","XOM","XRAY","XYL","YUM","ZBH","ZBRA",
        "ZION","ZTS",
    ]


def get_sp500_tickers() -> list[str]:
    """Try 3 sources for S&P 500 tickers, fallback gracefully."""
    print("📋 Fetching S&P 500 ticker list...")

    # Method 1: Wikipedia (with proper User-Agent to avoid 403)
    print("   1) Wikipedia...", end=" ", flush=True)
    tickers = _fetch_from_wikipedia()
    if tickers:
        print(f"✅ {len(tickers)} tickers")
        return tickers

    # Method 2: GitHub datasets repo
    print("   2) GitHub CSV...", end=" ", flush=True)
    tickers = _fetch_from_github()
    if tickers:
        print(f"✅ {len(tickers)} tickers")
        return tickers

    # Method 3: Hardcoded fallback
    print("   3) Using built-in list...", end=" ", flush=True)
    tickers = _hardcoded_sp500()
    print(f"✅ {len(tickers)} tickers (may not be 100% current)")
    return tickers


# ═══════════════════════════════════════════════════════════════════════════════
# INSIDER TRADE FETCHING
# ═══════════════════════════════════════════════════════════════════════════════

def _classify_trade(transaction_text: str) -> str:
    """Classify a transaction as Buy, Sell, or Other based on its text."""
    t = str(transaction_text).lower()
    if any(kw in t for kw in ("purchase", "buy", "acquisition")):
        return "Buy"
    elif any(kw in t for kw in ("sale", "sell", "disposition")):
        return "Sell"
    return "Other"


def fetch_insider_trades(ticker: str) -> pd.DataFrame | None:
    """
    Fetch insider transactions for one ticker via yfinance.
    Returns filtered DataFrame (>= $1M trades) or None.

    yfinance v1.1.0 insider_transactions columns:
      Start Date | Insider | Position | URL | Transaction | Text | Shares | Value | Ownership
    
    'Value' is numeric (float) — total dollar value of the transaction.
    """
    t = yf.Ticker(ticker)
    trades = t.insider_transactions  # single API call per ticker

    if trades is None or trades.empty:
        return None

    if "Value" not in trades.columns:
        return None

    trades["Value"] = pd.to_numeric(trades["Value"], errors="coerce")
    big_trades = trades[trades["Value"].abs() >= MIN_TRADE_VALUE].copy()

    if big_trades.empty:
        return None

    big_trades.insert(0, "Ticker", ticker)
    big_trades["Trade_Type"] = big_trades["Transaction"].apply(_classify_trade)
    big_trades.drop(columns=["URL"], errors="ignore", inplace=True)
    return big_trades


def rate_limited_fetch(ticker: str, attempt: int = 1) -> pd.DataFrame | None:
    """Wraps fetch with exponential backoff on rate-limit (429) errors."""
    try:
        return fetch_insider_trades(ticker)
    except Exception as e:
        err_str = str(e).lower()
        is_rate_limit = "429" in err_str or "too many" in err_str
        is_retryable = "timeout" in err_str or "connection" in err_str

        if (is_rate_limit or is_retryable) and attempt <= MAX_RETRIES:
            wait = (2 ** attempt) * 10 + random.uniform(0, 5)
            print(
                f"\n   ⚠️  {'Rate limited' if is_rate_limit else 'Connection error'} "
                f"on {ticker}. Retrying in {wait:.0f}s ({attempt}/{MAX_RETRIES})..."
            )
            time.sleep(wait)
            return rate_limited_fetch(ticker, attempt + 1)
        elif attempt > MAX_RETRIES:
            print(f"\n   ❌ Max retries exceeded for {ticker}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# CHECKPOINT / RESUME
# ═══════════════════════════════════════════════════════════════════════════════

def load_checkpoint() -> dict:
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, "r") as f:
            return json.load(f)
    return {"completed": [], "failed": []}


def save_checkpoint(completed: list[str], failed: list[str]):
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump({"completed": completed, "failed": failed}, f)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def _load_existing_data() -> pd.DataFrame | None:
    """Load existing CSV output if it exists."""
    if os.path.exists(OUTPUT_FILE):
        try:
            df = pd.read_csv(OUTPUT_FILE)
            if "Start Date" in df.columns:
                df["Start Date"] = pd.to_datetime(df["Start Date"], errors="coerce")
            # Backfill Trade_Type for old data that doesn't have it yet
            if "Trade_Type" not in df.columns and "Transaction" in df.columns:
                df["Trade_Type"] = df["Transaction"].apply(_classify_trade)
            return df
        except Exception:
            return None
    return None


def _latest_dates_per_ticker(df: pd.DataFrame) -> dict[str, datetime]:
    """Return {ticker: latest_date} from an existing DataFrame."""
    if df is None or df.empty or "Start Date" not in df.columns:
        return {}
    df["Start Date"] = pd.to_datetime(df["Start Date"], errors="coerce")
    return df.dropna(subset=["Start Date"]).groupby("Ticker")["Start Date"].max().to_dict()


def _print_summary(final: pd.DataFrame, tickers: list[str],
                   elapsed_total: float, failed_tickers: list[str],
                   mode_label: str):
    """Print the final summary report."""
    print(f"  ✅ {mode_label} COMPLETE!")
    print(f"     Tickers scanned:           {len(tickers)}")
    print(f"     Tickers with $1M+ trades:  {final['Ticker'].nunique()}")
    print(f"     Total $1M+ transactions:   {len(final)}")
    print(f"     Run time:                  {elapsed_total / 60:.1f} min")
    print(f"     Output:                    {OUTPUT_FILE}")

    if failed_tickers:
        with open(FAILED_LOG, "w") as f:
            f.write("\n".join(failed_tickers))
        print(f"     Failed tickers:            {FAILED_LOG} ({len(failed_tickers)})")

    print()
    print("─── TOP 15 LARGEST INSIDER TRADES ─────────────────────────────")
    top = final.head(15)
    display_cols = [
        "Ticker", "Insider", "Position", "Start Date",
        "Trade_Type", "Transaction", "Shares", "Value",
    ]
    display_cols = [c for c in display_cols if c in top.columns]
    print(top[display_cols].to_string(index=False))

    print()
    print("─── TRADES PER TICKER (top 10 by total value) ─────────────────")
    ticker_counts = final.groupby("Ticker").agg(
        trades=("Value", "count"),
        total_value=("Value", lambda x: x.abs().sum()),
        max_trade=("Value", lambda x: x.abs().max()),
    ).sort_values("total_value", ascending=False)
    print(ticker_counts.head(10).to_string())


def run_full_scrape(tickers: list[str]):
    """Original full-scrape mode with checkpoint/resume support."""
    checkpoint = load_checkpoint()
    already_done = set(checkpoint.get("completed", []))
    failed_tickers = list(checkpoint.get("failed", []))

    if already_done:
        print(f"\n🔄 Resuming from checkpoint: {len(already_done)} tickers already done")

    remaining = [t for t in tickers if t not in already_done]
    avg_delay = (DELAY_MIN + DELAY_MAX) / 2
    est_min = len(remaining) * avg_delay / 60

    print(f"\n📊 Tickers remaining: {len(remaining)} / {len(tickers)}")
    print(f"⏱️  Estimated time: ~{est_min:.0f} minutes")
    print(f"📁 Output file: {OUTPUT_FILE}")
    print()

    all_results: list[pd.DataFrame] = []
    completed = list(already_done)
    tickers_with_big_trades = 0
    total_big_trades = 0

    # Load existing results if resuming
    if already_done and os.path.exists(OUTPUT_FILE):
        try:
            existing = pd.read_csv(OUTPUT_FILE)
            all_results.append(existing)
            total_big_trades = len(existing)
            tickers_with_big_trades = existing["Ticker"].nunique()
            print(f"   📂 Loaded {len(existing)} trades from previous run\n")
        except Exception:
            pass

    start_time = time.time()

    for i, ticker in enumerate(remaining, 1):
        elapsed = time.time() - start_time
        rate = (i / max(elapsed, 1)) * 3600

        pct = i / len(remaining) * 100
        eta_sec = (len(remaining) - i) * avg_delay
        eta_str = f"{eta_sec/60:.0f}m" if eta_sec > 60 else f"{eta_sec:.0f}s"

        print(
            f"  [{i:>3}/{len(remaining)}] {pct:5.1f}%  {ticker:<6}  "
            f"(~{rate:.0f} req/hr, ETA {eta_str})  ",
            end="",
            flush=True,
        )

        result = rate_limited_fetch(ticker)

        if result is not None and not result.empty:
            n = len(result)
            max_val = result["Value"].abs().max()
            all_results.append(result)
            tickers_with_big_trades += 1
            total_big_trades += n
            print(f"✅ {n} trade(s)  (max ${max_val:,.0f})")
        else:
            print("—")

        completed.append(ticker)

        # Periodic checkpoint
        if i % CHECKPOINT_EVERY == 0 or i == len(remaining):
            save_checkpoint(completed, failed_tickers)
            if all_results:
                combined = pd.concat(all_results, ignore_index=True)
                combined.drop_duplicates(inplace=True)
                combined.to_csv(OUTPUT_FILE, index=False)
                print(
                    f"        💾 Checkpoint: {len(completed)}/{len(tickers)} tickers, "
                    f"{total_big_trades} trades saved"
                )

        # Throttle
        delay = random.uniform(DELAY_MIN, DELAY_MAX)
        time.sleep(delay)

    # ─── FINAL OUTPUT ────────────────────────────────────────────────────────
    print()
    print("=" * 65)

    if all_results:
        final = pd.concat(all_results, ignore_index=True)
        final.drop_duplicates(inplace=True)
        final["_abs_val"] = final["Value"].abs()
        final.sort_values("_abs_val", ascending=False, inplace=True)
        final.drop(columns=["_abs_val"], inplace=True)
        final.to_csv(OUTPUT_FILE, index=False)

        _print_summary(final, tickers, time.time() - start_time, failed_tickers, "FULL SCRAPE")
    else:
        print("  ⚠️  No $1M+ insider trades found.")

    # Clean up checkpoint
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)

    print()
    print(f"📁 Full results: {OUTPUT_FILE}")
    print()


def run_daily_update(tickers: list[str]):
    """
    Daily update mode: load existing data, fetch only new trades per ticker,
    and append them.  Tickers with no new data are skipped quickly.
    """
    existing_df = _load_existing_data()
    if existing_df is not None and not existing_df.empty:
        latest_dates = _latest_dates_per_ticker(existing_df)
        print(f"\n📂 Loaded {len(existing_df)} existing trades ({existing_df['Ticker'].nunique()} tickers)")
    else:
        latest_dates = {}
        existing_df = None
        print("\n📂 No existing data found — will do a full fetch")

    avg_delay = (DELAY_MIN + DELAY_MAX) / 2
    est_min = len(tickers) * avg_delay / 60
    print(f"📊 Checking {len(tickers)} tickers for new trades")
    print(f"⏱️  Estimated time: ~{est_min:.0f} minutes")
    print()

    new_rows: list[pd.DataFrame] = []
    failed_tickers: list[str] = []
    start_time = time.time()

    for i, ticker in enumerate(tickers, 1):
        elapsed = time.time() - start_time
        rate = (i / max(elapsed, 1)) * 3600
        pct = i / len(tickers) * 100
        eta_sec = (len(tickers) - i) * avg_delay
        eta_str = f"{eta_sec/60:.0f}m" if eta_sec > 60 else f"{eta_sec:.0f}s"

        print(
            f"  [{i:>3}/{len(tickers)}] {pct:5.1f}%  {ticker:<6}  "
            f"(~{rate:.0f} req/hr, ETA {eta_str})  ",
            end="",
            flush=True,
        )

        result = rate_limited_fetch(ticker)

        if result is not None and not result.empty:
            # Filter to only new trades (after the latest date we already have)
            cutoff = latest_dates.get(ticker)
            if cutoff is not None:
                result["Start Date"] = pd.to_datetime(result["Start Date"], errors="coerce")
                new_only = result[result["Start Date"] > cutoff]
            else:
                new_only = result

            if not new_only.empty:
                n = len(new_only)
                max_val = new_only["Value"].abs().max()
                new_rows.append(new_only)
                print(f"🆕 {n} new trade(s)  (max ${max_val:,.0f})")
            else:
                print("— up to date")
        else:
            print("—")

        # Throttle
        delay = random.uniform(DELAY_MIN, DELAY_MAX)
        time.sleep(delay)

    # ─── MERGE & SAVE ────────────────────────────────────────────────────────
    print()
    print("=" * 65)

    parts = []
    if existing_df is not None and not existing_df.empty:
        parts.append(existing_df)
    parts.extend(new_rows)

    if parts:
        final = pd.concat(parts, ignore_index=True)
        final.drop_duplicates(inplace=True)
        final["_abs_val"] = final["Value"].abs()
        final.sort_values("_abs_val", ascending=False, inplace=True)
        final.drop(columns=["_abs_val"], inplace=True)
        final.to_csv(OUTPUT_FILE, index=False)

        total_new = sum(len(r) for r in new_rows)
        print(f"  🆕 New trades added: {total_new}")
        _print_summary(final, tickers, time.time() - start_time, failed_tickers, "DAILY UPDATE")
    else:
        print("  ⚠️  No trades found (existing or new).")

    print()
    print(f"📁 Full results: {OUTPUT_FILE}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="S&P 500 Insider Trades Scraper ($1M+ transactions)",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help=(
            "Daily update mode: load existing CSV and only fetch new trades "
            "that are more recent than what's already saved. Without this flag, "
            "a full scrape is performed."
        ),
    )
    args = parser.parse_args()

    print()
    print("=" * 65)
    print("  S&P 500 Insider Trades Scraper")
    print(f"  Filter: transactions >= ${MIN_TRADE_VALUE:,.0f}")
    print(f"  Mode:   {'DAILY UPDATE' if args.update else 'FULL SCRAPE'}")
    print(f"  Date:   {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 65)

    tickers = get_sp500_tickers()

    if args.update:
        run_daily_update(tickers)
    else:
        run_full_scrape(tickers)


if __name__ == "__main__":
    main()
