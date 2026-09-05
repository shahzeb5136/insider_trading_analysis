# Insider Trading Service — Backend

Turns the briefing in `reports/` into a paid service: one credit buys the
day's PDF, built from that morning's SEC filing snapshot.

## How it works

The briefing is **market-wide**, so its output is identical for every user on
a given day. Generating it per click would burn minutes of CPU to produce
bytes someone else already has. So it is pre-built instead:

```
  11:00 UTC daily
        │
        ├─ SEC daily indexes      → new Form 4 filings          ~25s
        ├─ a slice of the gap     → older days not yet covered  ~11 min
        ├─ build the PDF          in a subprocess                ~5s
        ├─ upload to R2           insider/reports/<date>/<id>/
        └─ report row → 'ready'

  user clicks "Unlock today's briefing"
        │
        ├─ deduct 1 credit        shared Postgres, atomic
        ├─ record purchase        SQLite, unique per (user, report)
        └─ return a presigned URL             ~200ms
```

### Pieces

| File | Role |
|---|---|
| `api/main.py` | FastAPI app and all endpoints |
| `api/scheduler.py` | Daily schedule, spawns builds, uploads, records reports |
| `api/build_report.py` | Subprocess entrypoint: refresh trades → build → emit manifest |
| `api/database.py` | Credits (Postgres) + reports/purchases (SQLite) |
| `api/storage.py` | Cloudflare R2 upload and presigned URLs |
| `api/auth.py` | Clerk JWT verification |
| `api/settings.py` | All environment configuration |

## No seed, no bootstrap file

The price-data service needs an R2 seed because its history is a 250MB CSV
that cannot live in git and would take hours to re-download from Yahoo.

This service needs neither. SEC publishes the whole history as quarterly bulk
datasets, and a cold volume reloads ~28,000 qualifying transactions from them
in **under thirty seconds** — faster than downloading a seed would be. There
is no seed object, no bootstrap CSV, no `ALLOW_BULK_SEED` escape hatch, and
nothing to keep in sync.

```
  cold volume ──► 9 quarterly bulk ZIPs        ~27s
                        │
                        ├─► daily indexes, last 7 days      ~25s
                        └─► a slice of the gap between them  bounded per run
  warm volume ──► daily indexes + next gap slice
```

### The gap, and why it is filled a piece at a time

SEC publishes each quarterly dataset well after the quarter closes — as of
writing, 2026q1 is available and 2026q2 is not. So bulk plus a week of dailies
leaves a hole of up to five months in the middle of the history.

That hole is not cosmetic. A multi-insider purchase inside it simply does not
exist as far as the cluster analysis is concerned, which is the one thing the
report is for.

Closing it costs ~15 seconds per trading day, so doing it all at once would
block a cold start for the better part of an hour. Instead each build works
through `SEC_GAP_DAYS_PER_RUN` days, oldest first, recording progress per day
in `index_days`. The service is useful from the first build and reaches
complete history over the following few days. An interrupted run resumes
exactly where it stopped.

`GET /api/admin/status` reports `days_remaining` so you can see it closing.

### Schema migrations

`store.init_db()` runs `CREATE TABLE IF NOT EXISTS` and then an additive
column migration. This matters on redeploy: `IF NOT EXISTS` does nothing to a
table that already exists, so a new column would otherwise be missing on a
warm volume and every insert would fail. Migrations only ever add columns —
never drop or retype — so rolling back a release cannot lose data.

## Sharing the Yahoo Finance budget

The price-data service pulls OHLCV for ~500 tickers daily at 22:00 UTC. That
is a shared, rate-limited budget, and the two services must not fetch at once.

Switching this service to SEC EDGAR removed almost all of the contention —
trade data no longer touches Yahoo at all. What remains is the chart price
series: **one batched request per build**, negligible against the budget.

Two guards, both entirely inside this repo:

1. **Different build hours.** This service builds at 11:00 UTC, eleven hours
   clear of 22:00. 11:00 UTC is also ~7am ET, by which point the previous
   session's Form 4 filings have settled — EDGAR's cutoff is 10pm ET.
2. **A quiet window.** The service refuses to *start* a fetching build during
   `QUIET_HOURS_UTC` (21:00-23:59 by default), and retries as soon as the
   window clears. This covers what a build hour alone does not: `BUILD_ON_BOOT`
   firing during a redeploy, an admin rebuild, or a run that overruns.

The quiet-hours check happens **before** the report row is created. A skip
path that returned after creating the row would leave it stuck in `building`,
and the same-day guard would then suppress every retry for the rest of the day.

`POST /api/admin/build?skip_fetch=true` is always safe — it rebuilds the PDF
from the store without any network fetch at all.

## Deploying to Railway

1. **New service** from `shahzeb5136/insider_trading_analysis`.
   `railway.json` selects the Dockerfile.

2. **Attach a volume mounted at `/data`.** Required — it holds the trade
   store, the service SQLite DB, and the chart cache. 2GB is plenty. Note the
   path is `/data`; if you run another service with a different mount point,
   do not copy its `DATA_DIR` value here.

3. **Reference the existing Postgres** so credits are the shared wallet:
   ```
   DATABASE_URL=${{Postgres.DATABASE_URL}}
   ```
   Use the variable reference, not a pasted URL, so it survives rotation.

4. **Set the remaining variables** (see `.env.example`):
   `CLERK_JWKS_URL`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`,
   `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`, `ADMIN_SECRET_KEY`,
   `FRONTEND_URL`, and **`SEC_USER_AGENT`**.

   `CLERK_JWKS_URL` **must be the same Clerk application** as the other
   services — that is what makes the user IDs match and the wallet shared.

   `SEC_USER_AGENT` must carry a real contact address. SEC refuses requests
   without one, so a wrong value here fails every fetch.

5. **First boot** loads the bulk history, catches up the last week, closes the
   first slice of the gap, and builds the first report — roughly fifteen
   minutes. The API is live and healthy throughout; `/api/insider/latest`
   returns `report: null, building: true` until it lands.

The R2 bucket must exist and should stay private — the service hands out
short-lived presigned URLs rather than public links. Sharing a bucket with the
other services is fine: keys here live under `insider/`.

### Frontend

Set `NEXT_PUBLIC_INSIDER_API_URL` to the deployed service URL. The dashboard
page is at `/dashboard/insider-trading`; with the variable unset it falls back
to `http://localhost:8003` for local work.

### Environment variables

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | — | Shared Postgres. Unset ⇒ SQLite (local dev only) |
| `CLERK_JWKS_URL` | — | **Required.** Same Clerk app as the other services |
| `SEC_USER_AGENT` | placeholder | **Required.** Must carry a real contact address |
| `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | — | **Required** |
| `R2_BUCKET_NAME` | `tradingagents` | Must exist, keep private |
| `R2_PREFIX` | `insider` | Key prefix inside the shared bucket |
| `ADMIN_SECRET_KEY` | — | Protects `/api/admin/*`. Long random string |
| `FRONTEND_URL` | `http://localhost:3000` | CORS origin |
| `EXTRA_CORS_ORIGINS` | — | Comma-separated extras |
| `REPORT_CREDIT_COST` | `1` | Credits per briefing |
| `BUILD_HOUR_UTC` | `11` | ~7am ET, after filings settle |
| `QUIET_HOURS_UTC` | `21,22,23` | Never start a fetching build in these hours |
| `BUILD_ON_BOOT` | `true` | Build immediately if no report is ready |
| `SCHEDULER_ENABLED` | `true` | See scaling note below |
| `BUILD_TIMEOUT_SECONDS` | `3600` | Ceiling on one build |
| `SEC_GAP_DAYS_PER_RUN` | `45` | Trading days of the gap closed per build |
| `SEC_LOOKBACK_DAYS` | `7` | Days of daily index re-scanned each run |
| `DOWNLOAD_URL_TTL_SECONDS` | `3600` | Presigned URL lifetime |
| `DATA_DIR` | `/data` | Set by the Dockerfile |

## API

All user endpoints take `Authorization: Bearer <clerk_jwt>`.

### `GET /api/insider/catalogue` — public

What a briefing contains. No auth, safe for a landing page.

### `GET /api/insider/latest`

Drives the button. Tells you the edition on sale, the user's balance, whether
they already own it, and the headline figures.

```json
{
  "report": {
    "id": "93dc2b82-…", "snapshot_date": "2026-09-05",
    "data_through": "2026-09-03", "built_at": "2026-09-05T11:04:11+00:00",
    "trade_count": 28273, "ticker_count": 492,
    "cluster_count": 6, "new_cluster_count": 0,
    "purchase_count": 18, "purchase_value": 247599812.17,
    "sale_value": 827666592.67, "window_days": 10,
    "files": [ { "slug": "insider", "title": "Insider Trading Briefing",
                 "filename": "insider_report_2026-09-05.pdf",
                 "bytes": 454835, "kind": "PDF" } ]
  },
  "owned": false, "credit_cost": 1, "credits": 18,
  "downloads": [], "building": false, "download_ttl_seconds": 3600
}
```

The aggregate figures are returned **before** purchase on purpose: they make
the credit an informed spend, and they give away no names.

- `owned: false` → show the price, enable the button.
- `owned: true` → `downloads` is populated; show links, no charge.
- `report: null` + `building: true` → first build still running.

### `POST /api/insider/purchase`

Body is optional: `{ "report_id": "93dc2b82-…" }`. Passing the id the user was
shown is recommended — if the daily rebuild lands between render and click you
get a `409` instead of silently selling a different edition.

**Charging is idempotent per (user, report).** Calling it again for an edition
the user already owns returns `charged: false`, `credits_spent: 0`, and fresh
links. A double-click, a retry, or a refresh cannot double-charge.

| Status | Meaning |
|---|---|
| `402` | Insufficient credits — send them to the top-up flow |
| `409` | No report ready yet, or the pinned `report_id` is stale |

### `GET /api/insider/purchases`

Every edition the user owns, newest first, each with freshly signed links.

### `GET /api/insider/reports/{report_id}/downloads`

Re-sign links for an owned edition. `403` if the user does not own it.

### `GET /api/credits`

`{ "credits": 18 }` — the shared balance, the same number the other services show.

### Admin — `?key=<ADMIN_SECRET_KEY>`

| Endpoint | Purpose |
|---|---|
| `GET /api/admin/status` | Scheduler state, quiet-hours state, latest report, store stats |
| `GET /api/admin/reports?limit=25` | Report history including failures |
| `GET /api/admin/users` | All users and balances |
| `POST /api/admin/credits` | `{"user_id": "...", "amount": 5}` — grant credits |
| `POST /api/admin/build` | Force a rebuild now |

`POST /api/admin/build?key=…&skip_fetch=true` rebuilds the PDF from the store
as it stands, without any network fetch. That is the fast way to reissue a
report after changing report code, and it is always safe with respect to the
shared Yahoo budget. `force=true` overrides the quiet-hours guard.

## Local development

```bash
pip install -r requirements.txt
cp .env.example .env      # set SEC_USER_AGENT; leave DATABASE_URL blank for SQLite
```

Serve the API without ever building:

```bash
SCHEDULER_ENABLED=false python main.py serve
```

Build a report from the existing store without touching the network:

```bash
python main.py report --out ./out.pdf
```

## Operational notes

- **One builder only.** The scheduler assumes a single process owns the daily
  build. If you scale past one replica, set `SCHEDULER_ENABLED=false` on the
  extras.
- **Reports are never deleted.** Purchases reference them, so old links keep
  working. At ~450KB/day that is ~160MB/year of R2.
- **A failed build leaves the previous report on sale.**
  `get_latest_ready_report()` only ever returns `ready` rows, so a bad morning
  degrades to a stale briefing rather than an outage. Watch
  `GET /api/admin/status` for `last_error`.
- **Interrupted builds self-heal.** A report left `building` by a redeploy is
  marked failed on next boot, so the same-day guard cannot wedge.
- **Credit refunds.** Credits live in Postgres and purchases in SQLite, so the
  two writes cannot share a transaction. The credit is taken first and
  refunded if the purchase row fails to land.
- **Memory.** The build subprocess holds the trade history and several price
  series in pandas — a few hundred MB. It is deliberately a separate process
  so an OOM cannot take the API down.
- **Filer typos.** Form 4s are self-reported and a handful carry impossible
  dates (years like `0024`, or dates in 2028). They are dropped on ingest —
  four in the last 28,000 — because a 2028 transaction would sit at the top of
  "most recent" permanently.
