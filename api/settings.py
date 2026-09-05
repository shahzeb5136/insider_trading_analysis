"""Environment-driven settings for the insider report service.

Everything the service needs to know about *where* things live is resolved
here so the rest of the package never touches ``os.environ`` directly. Data
paths come from the root ``config`` module, which the fetcher and the report
builder also read — one definition, so the API and the build subprocess can
never disagree about where the trade store is.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from config import DATA_DIR, TRADES_DB_PATH

# ── Filesystem ───────────────────────────────────────────────────────────────

# Service-specific tables (reports, purchases). Credits live in Postgres.
SQLITE_PATH = DATA_DIR / "insider_service.db"

# Scratch space for a report build before the PDF is uploaded to R2.
BUILD_WORK_DIR = DATA_DIR / "builds"

# ── Storage ──────────────────────────────────────────────────────────────────

# Prefix inside the shared R2 bucket. The bucket also holds trading_agents'
# `jobs/` and the price service's `packs/` and `seed/`; this service only ever
# writes under `insider/`, so the three cannot collide.
R2_PREFIX = os.getenv("R2_PREFIX", "insider")

# ── Pricing ──────────────────────────────────────────────────────────────────

# Credits burned per report. Kept configurable so the products sharing the
# wallet can be repriced relative to each other without a migration.
REPORT_CREDIT_COST = int(os.getenv("REPORT_CREDIT_COST", "1"))

# ── Build schedule ───────────────────────────────────────────────────────────

# Hour (UTC) for the daily fetch + build.
#
# 11:00 UTC is ~7am ET, and the choice is driven by two things. Form 4s are
# due within two business days and post to EDGAR through the day up to a
# 10pm ET cutoff (02:00-03:00 UTC), so a morning build sees the previous
# session's filings complete rather than half-arrived. And it sits 11 hours
# from the price service's 22:00 UTC build, which matters because the two
# share one Yahoo Finance request budget.
BUILD_HOUR_UTC = int(os.getenv("BUILD_HOUR_UTC", "11"))

# Hours (UTC) during which this service will not start a Yahoo-fetching
# build, because the price-data service is working. Separate build hours
# already keep the scheduled runs apart; this closes the other doors —
# BUILD_ON_BOOT firing during a redeploy, an admin rebuild, or a run that
# overruns. A build deferred by this window is retried on the next poll.
QUIET_HOURS_UTC = os.getenv("QUIET_HOURS_UTC", "21,22,23")

# How often the scheduler thread wakes to check whether a build is due.
SCHEDULER_POLL_SECONDS = int(os.getenv("SCHEDULER_POLL_SECONDS", "300"))

# Hard ceiling on a single build. A full fetch of ~500 tickers plus the PDF
# runs in a few minutes; this only exists so a wedged network call cannot
# block every subsequent build forever.
BUILD_TIMEOUT_SECONDS = int(os.getenv("BUILD_TIMEOUT_SECONDS", "3600"))

# Build a report on boot if none is ready yet. Disable for local API-only work.
BUILD_ON_BOOT = os.getenv("BUILD_ON_BOOT", "true").lower() == "true"

# Run the scheduler at all. Turn off to serve the API without ever building —
# useful for local frontend work, and required if you ever scale past one
# replica, since exactly one process should own the daily build.
SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "true").lower() == "true"

# Force a check of every ticker rather than the adaptive subset. Off by
# default: the adaptive schedule is what keeps the daily Yahoo spend low.
FETCH_FULL_UNIVERSE = os.getenv("FETCH_FULL_UNIVERSE", "false").lower() == "true"

# ── Downloads ────────────────────────────────────────────────────────────────

# Lifetime of the presigned R2 URLs handed to the browser. The frontend
# re-signs rather than caching, so this can stay short.
DOWNLOAD_URL_TTL_SECONDS = int(os.getenv("DOWNLOAD_URL_TTL_SECONDS", "3600"))

# ── Web ──────────────────────────────────────────────────────────────────────

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

ADMIN_SECRET_KEY = os.getenv("ADMIN_SECRET_KEY", "")


def quiet_hours() -> set[int]:
    """Parse ``QUIET_HOURS_UTC`` into a set of hours. Empty disables the guard.

    Unparseable entries are logged rather than skipped silently. The failure
    mode this protects against is quiet: setting the variable to a range like
    ``21-23`` instead of ``21,22,23`` parses to nothing, which reads as "no
    quiet hours configured" and removes the whole Yahoo-budget guard without
    anything appearing to be wrong.
    """
    hours: set[int] = set()
    rejected: list[str] = []

    for chunk in QUIET_HOURS_UTC.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            hour = int(chunk)
        except ValueError:
            rejected.append(chunk)
            continue
        if 0 <= hour <= 23:
            hours.add(hour)
        else:
            rejected.append(chunk)

    if rejected:
        logging.getLogger(__name__).warning(
            "QUIET_HOURS_UTC=%r contains %s that is not an hour 0-23: %s. "
            "Expected a comma-separated list, e.g. '21,22,23'.%s",
            QUIET_HOURS_UTC,
            "entries" if len(rejected) > 1 else "an entry",
            ", ".join(rejected),
            "" if hours else " No quiet hours are in force.",
        )

    return hours


def allowed_origins() -> list[str]:
    """CORS allow-list: the configured frontend plus local dev."""
    origins = {FRONTEND_URL, "http://localhost:3000"}
    extra = os.getenv("EXTRA_CORS_ORIGINS", "")
    origins.update(o.strip() for o in extra.split(",") if o.strip())
    return sorted(origins)


def ensure_dirs() -> None:
    """Create the volume directories the service writes to."""
    from config import ensure_dirs as ensure_data_dirs

    ensure_data_dirs()
    BUILD_WORK_DIR.mkdir(parents=True, exist_ok=True)
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
