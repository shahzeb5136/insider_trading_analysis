# Superseded scripts

The two scripts here are the original pipeline, kept for reference. Neither is
imported by the service and neither is run by anything.

| File | Replaced by | Why |
|---|---|---|
| `sp500_insider_trades.py` | `sec_fetcher.py` | Read Yahoo Finance one ticker at a time with a 2-5s sleep between each — ~30 minutes for a run whose requests totalled two minutes. Yahoo also caps insider history at 150 rows per ticker and runs a median ~18 days behind. |
| `report_gen.py` | `reports/` | Emitted Markdown and a `.docx`, ran to 40+ pages, opened with database metadata, and re-downloaded the S&P 500 benchmark once per chart. |

The current pipeline reads SEC EDGAR directly, classifies trades by the
filer's own transaction code rather than by grepping prose, and renders a
reportlab PDF in the same house style as the other NexGen reports.

The token bucket and the S&P 500 constituent lookup those scripts needed now
live in `ratelimit.py` and `universe.py` at the repo root. Yahoo remains the
source for the report's chart price series only.
