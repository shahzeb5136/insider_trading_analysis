# Insider Trading Briefing

A daily PDF briefing on S&P 500 insider transactions of $1M or more, built
from SEC EDGAR and sold for one credit through the NexGen dashboard.

The product question is narrow: **did anyone who runs one of these companies
put their own money into its stock, and does anyone else agree with them?**
Most insider activity is noise — executives sell constantly, on schedules set
months in advance, for reasons that have nothing to do with their view of the
business. So the report leads with the two things that are not noise:

* **Open-market purchases.** An insider buying at the market price, with their
  own money, under no obligation to.
* **Clusters.** Several insiders at the same company buying on the same day.
  Hard to explain as coincidence or personal liquidity.

Each cluster is scored for conviction and carries the stock's total return
since the purchase date against the S&P 500 — so the report is a track record,
not just a list of trades.

---

## Where the data comes from

**SEC EDGAR**, read directly. Not a market-data vendor.

That is a deliberate change from how this project started. It used to read
Yahoo Finance's `insider_transactions` endpoint, which is convenient and free
and — measured against EDGAR on 2026-09-05 — not good enough:

| | Yahoo | SEC EDGAR |
|---|---|---|
| History per ticker | Hard-capped at **150 rows** | Complete |
| Freshness | Median **~18 days** behind (XOM was 173 days stale) | Same day |
| Classification | Inferred by grepping prose for "purchase" | The filer's own **transaction code** |
| 10b5-1 plan flag | Absent | `aff10b5One`, stated outright |
| Coverage | Missed $55M of Cascade Investment buying RSG | Had it on the filing date |

The classification difference matters more than it sounds. EDGAR gives every
transaction a code — `P` for an open-market purchase, `S` for a sale, `M` for
an option exercise, `A` for a grant, `F` for shares withheld to pay tax. The
old text heuristic could not reliably separate a purchase from an option
exercise that happened to be described as an acquisition, and it had no way at
all to tell a pre-arranged 10b5-1 disposal from a discretionary one.

### Three fetch paths

```
  cold store  ──►  quarterly bulk ZIPs        ~10MB each, ~1s to parse
                   9 quarters ≈ 28,000 qualifying transactions in ~27s

  the gap     ──►  daily filing indexes       SEC publishes bulk in arrears,
                   worked through oldest-      so there is a hole of up to five
                   first, a chunk per build    months between bulk and today

  daily run   ──►  daily filing index         1 request per calendar day
                   └─► Form 4 XML for covered issuers only
```

Only filings whose issuer CIK is in the S&P 500 are downloaded — the index
lists ~2,000 Form 4s a day across the whole market, of which ~120 are ours. A
`seen_filings` table means the week-long overlap that catches late and amended
filings does not re-download anything.

Issuers are matched on **CIK**, never on the ticker string. A dual-class
company is one CIK with two tickers (Alphabet is GOOG and GOOGL), and every one
of its filings names whichever class traded; matching on the string dropped all
53 Alphabet filings — $422M — in one quarter. Each row's identity is the
filing's accession number plus the line's content plus its occurrence index
among identical lines, so the same trade arriving from the bulk TSV (prices to
2 decimals) and the Form 4 XML (prices to 4) is stored once, and two identical
$2M lots in one filing are stored twice.

Requests follow SEC's published policy: a declared User-Agent carrying a real
contact address, and no more than 10 per second. This runs at 8.

---

## Commands

```bash
pip install -r requirements.txt
cp .env.example .env        # set SEC_USER_AGENT to a real contact address

python main.py fetch        # incremental: daily indexes + a slice of the gap
python main.py fetch --full # force a bulk backfill as well
python main.py report       # build the PDF from the store, no network
python main.py stats        # what the store holds and how complete it is
python main.py serve        # run the API locally on :8003
```

---

## Speed

The original pipeline took about 30 minutes. Almost none of that was work:

| Stage | Before | Now |
|---|---|---|
| Fetch trades | ~29 min (500 tickers × 2-5s `sleep`) | ~25s daily, ~110s cold |
| Chart price data | 44 Yahoo calls (the benchmark 22 times) | **1** batched call |
| Chart rendering | Serial, every chart every run | Parallel, cached on disk |
| Build the report | Markdown → python-docx | reportlab, ~4s |
| **Total** | **~30 min** | **~2 min cold, ~40s warm** |

The 29 minutes was a `time.sleep(random.uniform(2, 5))` after every ticker.
It was not protecting anything — the requests themselves took 250ms — and
switching to EDGAR removed the need for it entirely.

---

## Layout

| File | Role |
|---|---|
| `sec_fetcher.py` | SEC EDGAR: bulk backfill, gap fill, daily incremental |
| `store.py` | SQLite trade store, dedup keys, schema migrations |
| `config.py` | Every tunable, resolved from the environment |
| `reports/analysis.py` | Clusters, conviction scoring, aggregates — pure pandas |
| `reports/charts.py` | Batched price download, cached parallel chart rendering |
| `reports/pdf_report.py` | The reportlab briefing |
| `reports/builder.py` | Ties analysis → prices → charts → PDF together |
| `universe.py` | The covered universe: current S&P 500 constituents, with fallbacks |
| `ratelimit.py` | The token bucket every fetch run shares |
| `api/` | FastAPI service — see [SERVICE.md](SERVICE.md) |
| `legacy/` | The superseded Yahoo-era scripts, kept for reference only |

Yahoo Finance survives in exactly one place: the charts' price series, one
batched request per build. Trade data never touches it.

---

## The report

Ten pages, structured as a briefing rather than a data dump:

1. **Cover** — headline figures, four sentences on what changed, the five
   strongest clusters on record with their returns, and what the window was
   made of.
2. **Purchases** — every open-market buy in the window, largest first.
3. **Conviction signals** — the eight highest-conviction clusters, each with
   its participants and a chart of the stock against the S&P 500 since.
4. **Cluster history** — every remaining cluster, same ranking, no charts.
5. **Flows and participants** — net flow by company, largest buyers, insiders
   adding on multiple days.
6. **Appendix** — the full tape: every purchase plus disposals over $5M.
7. **Definitions** — what each term means, and the disclaimer.

The predecessor ran to 40+ pages, opened with database metadata, and put a
usually-empty section on page one. Sales are ~90% of the rows and close to 0%
of the signal, so they are summarised up front and listed in the appendix.

### Conviction score

A 0-1 composite over the five things that separate a strong cluster from one
that merely qualifies:

| Weight | Factor |
|---|---|
| 30% | Number of participating insiders |
| 25% | Dollar size, log-scaled |
| 20% | Share held directly rather than through a trust or partnership |
| 15% | Seniority of the participants |
| 10% | Recency, decaying over a year |

It is what stops a $500M purchase by a 10% holder outranking a $4M purchase by
a CEO and a president buying together — which is exactly what a raw
dollar-value sort does, and exactly backwards.

---

## Disclaimer

This is a screening tool built from public SEC filings. It is not investment
advice, not a recommendation, and makes no claim to completeness. Insider
purchases are one input among many and are frequently wrong. Filings are
self-reported and can be amended. Verify anything you intend to act on against
the original filing on EDGAR.
