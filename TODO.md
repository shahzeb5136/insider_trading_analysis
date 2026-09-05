# TODO

The code is done and verified. What remains needs your accounts.

## Deploy

1. **Railway** — new service from `shahzeb5136/insider_trading_analysis`.
   Attach a volume at `/data` (2GB). Variables, with the two that are not
   like the others called out:

   ```
   DATABASE_URL=${{Postgres.DATABASE_URL}}       # the literal reference, not a pasted URL
   SEC_USER_AGENT=NexGen Solutions insider-research <your email>   # SEC refuses requests without a real contact
   CLERK_JWKS_URL=https://clerk.nexgen-solutions.org/.well-known/jwks.json
   FRONTEND_URL=https://nexgen-solutions.org
   R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY   # same as the other services
   R2_BUCKET_NAME=tradingagents
   ADMIN_SECRET_KEY=<python -c "import secrets; print(secrets.token_urlsafe(32))">
   ```

   Everything else has a sensible default (`.env.example` lists them all).
   First boot takes ~15 minutes; `/api/insider/latest` returns
   `report: null, building: true` until the first edition lands. Watch it
   with `curl -H "X-Admin-Key: …" https://<service>/api/admin/status`.

2. **Frontend** — the page is committed in `e:\website` but not pushed,
   because pushing deploys your live site and the page needs the backend URL
   first. Once the Railway service is up:

   ```bash
   cd e:/website && git push
   ```

   and set `NEXT_PUBLIC_INSIDER_API_URL=https://<service>.up.railway.app` on
   the frontend host.

3. **The history gap closes over ~3 daily builds.** SEC publishes bulk
   datasets a quarter in arrears, so a fresh store has a hole between the
   newest bulk quarter and the last week — currently April to August 2026.
   Each build closes 45 trading days (`SEC_GAP_DAYS_PER_RUN`); `days_remaining`
   in `/api/admin/status` shows it shrinking. Cluster counts are not final
   until it reaches 0. Set `SEC_GAP_DAYS_PER_RUN=150` to close it in one
   ~40-minute first build instead.

## Worth considering later

- **Widen the universe.** The SEC path filters by issuer CIK, so covering
  more than the S&P 500 is a one-list change, and the fetch cost scales with
  filings rather than tickers. Small caps are where insider buying carries
  the most signal.
- **Derivative transactions.** Only `NONDERIV_TRANS` is parsed; the bulk ZIPs
  also carry `DERIV_TRANS`, where option grants and exercises live in detail.
  Not needed for the purchase signal.
- **Amended filings (4/A).** A 4/A that restates a transaction is stored as
  a new row alongside the original. Rare, and the original was what the
  filer said at the time, but a strict "latest version wins" would need
  the amendment to be linked back to what it amends.
- **Cluster window.** `CLUSTER_LOOKBACK_DAYS=540` is an editorial choice for
  a daily briefing. Revisit once the gap has closed.

## Things to know

- **Google Drive breaks git in this repo.** Drive writes `desktop.ini` into
  every folder it syncs, including `.git/refs/`, and git then fails every
  fetch and push with `fatal: bad object refs/desktop.ini`. Before any git
  operation: `find .git -name "desktop.ini" -delete`. They come back.
- `data_sec/` and `data_v2/` are local test stores from the build session,
  gitignored. Delete them whenever; the service rebuilds its own on the
  Railway volume.
