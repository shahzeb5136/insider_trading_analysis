# S&P 500 Insider Trades Scraper

A Python tool that scrapes insider trading transactions for all S&P 500 companies, filtering for trades **≥ $1,000,000**. Built with [yfinance](https://github.com/ranaroussi/yfinance) — **no API key needed**.

---

## Features

- **Full S&P 500 coverage** — automatically fetches the current ticker list from Wikipedia, GitHub, or a built-in fallback
- **$1M+ trade filter** — surfaces only the most significant insider buys and sells
- **Trade type classification** — labels each transaction as Buy, Sell, or Other
- **Daily update mode** — incrementally fetches only new trades since the last run (no re-downloading)
- **Checkpoint & resume** — saves progress every 25 tickers; re-run to pick up where you left off
- **Rate-limit handling** — random delays + exponential backoff with jitter to stay within Yahoo Finance limits
- **Summary report** — prints the top 15 largest trades and a per-ticker breakdown after each run

---

## Requirements

- Python 3.10+
- Dependencies:
  ```
  yfinance
  pandas
  lxml
  html5lib
  ```

### Installation

```bash
pip install yfinance pandas lxml html5lib
```

---

## Usage

### Full Scrape

Fetches insider trades for all ~500 S&P 500 tickers from scratch. Estimated runtime: **25–45 minutes**.

```bash
python sp500_insider_trades.py
```

### Daily Update

Loads the existing CSV and only fetches trades newer than what's already saved — much faster for routine use.

```bash
python sp500_insider_trades.py --update
```

---

## Output

| File | Description |
|------|-------------|
| `insider_trades_1m_plus.csv` | All insider trades ≥ $1M, sorted by absolute value (largest first) |
| `failed_tickers.txt` | Tickers that failed after retries (if any) |

### CSV Columns

| Column | Description |
|--------|-------------|
| `Ticker` | Stock ticker symbol |
| `Insider` | Name of the insider |
| `Position` | Insider's role (e.g. CEO, CFO, Director) |
| `Start Date` | Date of the transaction |
| `Trade_Type` | Classified as **Buy**, **Sell**, or **Other** |
| `Transaction` | Raw transaction description from SEC filing |
| `Text` | Additional transaction details |
| `Shares` | Number of shares traded |
| `Value` | Dollar value of the transaction |
| `Ownership` | Ownership type (direct/indirect) |

---

## Configuration

Configurable constants at the top of `sp500_insider_trades.py`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MIN_TRADE_VALUE` | `1,000,000` | Minimum trade value to include |
| `DELAY_MIN` | `2.0s` | Minimum delay between API requests |
| `DELAY_MAX` | `5.0s` | Maximum delay between API requests |
| `MAX_RETRIES` | `3` | Retry attempts per ticker on rate-limit errors |
| `CHECKPOINT_EVERY` | `25` | Save progress every N tickers |

---

## How It Works

1. **Fetch S&P 500 tickers** via three fallback sources:
   - Wikipedia (List of S&P 500 companies)
   - GitHub datasets repo (updated weekly)
   - Hardcoded list (~490 tickers, early 2026)

2. **Pull insider transactions** for each ticker using `yfinance`, one API call per ticker

3. **Filter** for trades with absolute value ≥ $1M

4. **Classify** each trade as Buy, Sell, or Other based on the transaction text

5. **Save** results to CSV with periodic checkpointing

---

## License

This project is for personal/educational use. Insider trading data is sourced from publicly available SEC filings via Yahoo Finance.

---

## Disclaimer

This tool is for **informational and educational purposes only**. It does not constitute financial advice. Always do your own research before making investment decisions. Insider trading data is publicly available through SEC filings.

