"""FastAPI application for the insider trading report service.

One credit buys the current briefing: a single PDF built from that morning's
filing snapshot. Reports are pre-built by the scheduler, so a purchase is a
credit deduction plus a presigned R2 URL — no waiting, no job polling.

Credits are read from the shared Railway Postgres, the same wallet the
trading_agents and report-suite services spend, keyed by Clerk user ID.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

from api import database, scheduler
from api.auth import get_user_data_from_token
from api.database import InsufficientCredits
from api.scheduler import REPORT_DESCRIPTION, REPORT_SLUG, REPORT_TITLE
from api.settings import (
    ADMIN_SECRET_KEY,
    DOWNLOAD_URL_TTL_SECONDS,
    REPORT_CREDIT_COST,
    SCHEDULER_ENABLED,
    allowed_origins,
    ensure_dirs,
)
from api.storage import get_download_url

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    database.init_db()

    import store

    store.init_db()

    if SCHEDULER_ENABLED:
        scheduler.start_scheduler()
    else:
        logger.warning(
            "Scheduler disabled (SCHEDULER_ENABLED=false) — no reports will be built"
        )
    logger.info("Insider Trading API started")
    yield
    if SCHEDULER_ENABLED:
        scheduler.stop_scheduler()
    logger.info("Insider Trading API stopped")


app = FastAPI(title="Insider Trading API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Auth ─────────────────────────────────────────────────────────────────────


async def get_current_user(authorization: Optional[str] = Header(None)) -> str:
    """Verify the Clerk bearer token and return the user ID."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401, detail="Missing or malformed Authorization header"
        )
    try:
        user_id, email = get_user_data_from_token(authorization[7:])
    except Exception as exc:
        logger.warning("Auth failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    database.get_or_create_user(user_id, email=email)
    return user_id


def require_admin(key: str) -> None:
    if not ADMIN_SECRET_KEY or key != ADMIN_SECRET_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")


# ── Serialisation helpers ────────────────────────────────────────────────────


def _report_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    """Public metadata for a report — safe to show before purchase.

    The headline statistics are deliberately included: they are what makes
    the buy decision informed rather than blind, and they are aggregate
    figures that give away none of the report's actual content.
    """
    stats = report.get("stats") or {}
    return {
        "id": report["id"],
        "snapshot_date": report["snapshot_date"],
        "data_through": report.get("data_through"),
        "built_at": report.get("completed_at"),
        "trade_count": report.get("trade_count"),
        "ticker_count": report.get("ticker_count"),
        "cluster_count": report.get("cluster_count"),
        "purchase_count": report.get("purchase_count"),
        "new_cluster_count": stats.get("new_cluster_count"),
        "purchase_value": stats.get("purchase_value"),
        "sale_value": stats.get("sale_value"),
        "window_days": stats.get("window_days"),
        "files": [
            {
                "slug": slug,
                "title": meta.get("title"),
                "description": meta.get("description"),
                "filename": meta.get("filename"),
                "bytes": meta.get("bytes"),
                "kind": meta.get("kind", "PDF"),
            }
            for slug, meta in (report.get("report_keys") or {}).items()
        ],
    }


def _downloads_for(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Presigned download links. Only ever called for a report the user owns."""
    downloads = []
    for slug, meta in (report.get("report_keys") or {}).items():
        try:
            url = get_download_url(meta["key"], filename=meta.get("filename"))
        except Exception:
            logger.exception("Could not sign download for %s", meta.get("key"))
            continue
        downloads.append(
            {
                "slug": slug,
                "title": meta.get("title"),
                "filename": meta.get("filename"),
                "bytes": meta.get("bytes"),
                "kind": meta.get("kind", "PDF"),
                "url": url,
            }
        )
    return downloads


# ── Models ───────────────────────────────────────────────────────────────────


class PurchaseRequest(BaseModel):
    # Optional: pin the purchase to the report the user was shown, so a
    # rebuild landing mid-click cannot silently sell them a different one.
    report_id: Optional[str] = None


class AdminAddCreditsRequest(BaseModel):
    user_id: str
    amount: int


# ── Public endpoints ─────────────────────────────────────────────────────────


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/insider/catalogue")
async def catalogue():
    """What a report contains. Unauthenticated — usable on a landing page."""
    return {
        "credit_cost": REPORT_CREDIT_COST,
        "download_ttl_seconds": DOWNLOAD_URL_TTL_SECONDS,
        "reports": [
            {
                "slug": REPORT_SLUG,
                "title": REPORT_TITLE,
                "description": REPORT_DESCRIPTION,
                "kind": "PDF",
            }
        ],
    }


@app.get("/api/credits")
async def credits(user_id: str = Depends(get_current_user)):
    return {"credits": database.get_user_credits(user_id)}


@app.get("/api/insider/latest")
async def latest_report(user_id: str = Depends(get_current_user)):
    """The report currently on sale, plus whether this user already owns it.

    Drives the button state: ``owned`` true means hand back the download link
    for free, false means show the price.
    """
    report = database.get_latest_ready_report()
    state = scheduler.get_state()

    if not report:
        return {
            "report": None,
            "owned": False,
            "credit_cost": REPORT_CREDIT_COST,
            "credits": database.get_user_credits(user_id),
            "downloads": [],
            "building": state.get("building", False),
            "download_ttl_seconds": DOWNLOAD_URL_TTL_SECONDS,
        }

    owned = database.get_purchase(user_id, report["id"]) is not None
    return {
        "report": _report_summary(report),
        "owned": owned,
        "credit_cost": REPORT_CREDIT_COST,
        "credits": database.get_user_credits(user_id),
        "downloads": _downloads_for(report) if owned else [],
        "building": state.get("building", False),
        "download_ttl_seconds": DOWNLOAD_URL_TTL_SECONDS,
    }


@app.post("/api/insider/purchase")
async def purchase(
    body: Optional[PurchaseRequest] = None,
    user_id: str = Depends(get_current_user),
):
    """Spend credits on the current report and return a download link.

    Charging is idempotent per (user, report): buying a report the user
    already owns re-issues a fresh link without deducting again.
    """
    report = database.get_latest_ready_report()
    if not report:
        raise HTTPException(
            status_code=409,
            detail="No report is available yet. Please try again shortly.",
        )

    if body and body.report_id and body.report_id != report["id"]:
        raise HTTPException(
            status_code=409,
            detail="A newer report is available. Refresh and try again.",
        )

    try:
        purchase_row, charged = database.purchase_report(
            user_id, report["id"], REPORT_CREDIT_COST
        )
    except InsufficientCredits:
        raise HTTPException(
            status_code=402,
            detail="Insufficient credits. Please purchase more credits.",
        )

    if charged:
        logger.info(
            "User %s bought report %s for %s credit(s)",
            user_id, report["id"], REPORT_CREDIT_COST,
        )

    return {
        "purchase_id": purchase_row["id"],
        "report": _report_summary(report),
        "charged": charged,
        "credits_spent": REPORT_CREDIT_COST if charged else 0,
        "credits_remaining": database.get_user_credits(user_id),
        "downloads": _downloads_for(report),
    }


@app.get("/api/insider/purchases")
async def purchases(user_id: str = Depends(get_current_user)):
    """Every report this user owns, with freshly signed download links."""
    rows = database.list_user_purchases(user_id)
    return {
        "purchases": [
            {
                "purchase_id": row["purchase_id"],
                "purchased_at": row["purchased_at"],
                "credits_spent": row["credits_spent"],
                "report": _report_summary(row),
                "downloads": _downloads_for(row),
            }
            for row in rows
        ]
    }


@app.get("/api/insider/reports/{report_id}/downloads")
async def report_downloads(report_id: str, user_id: str = Depends(get_current_user)):
    """Re-sign links for a report the user already owns.

    Presigned URLs expire, so the frontend calls this rather than caching them.
    """
    if not database.get_purchase(user_id, report_id):
        raise HTTPException(status_code=403, detail="You do not own this report")

    report = database.get_report(report_id)
    if not report or report["status"] != "ready":
        raise HTTPException(status_code=404, detail="Report not found")

    return {"report": _report_summary(report), "downloads": _downloads_for(report)}


# ── Admin endpoints ──────────────────────────────────────────────────────────


@app.get("/api/admin/users")
async def admin_users(key: str):
    require_admin(key)
    return {"users": database.get_all_users()}


@app.post("/api/admin/credits")
async def admin_add_credits(body: AdminAddCreditsRequest, key: str):
    require_admin(key)
    return {
        "user_id": body.user_id,
        "new_balance": database.add_credits(body.user_id, body.amount),
    }


@app.get("/api/admin/reports")
async def admin_reports(key: str, limit: int = 25):
    require_admin(key)
    return {"reports": database.list_reports(limit=limit)}


@app.get("/api/admin/status")
async def admin_status(key: str):
    require_admin(key)

    import store

    latest = database.get_latest_ready_report()
    return {
        "scheduler": scheduler.get_state(),
        "in_quiet_hours": scheduler.in_quiet_hours(),
        "latest_report": _report_summary(latest) if latest else None,
        "credit_cost": REPORT_CREDIT_COST,
        "trade_store": store.stats(),
    }


@app.post("/api/admin/build")
async def admin_build(key: str, skip_fetch: bool = False, force: bool = False):
    """Force a rebuild now.

    ``skip_fetch=true`` rebuilds the PDF from the trade store as it stands,
    without hitting Yahoo Finance — the fast way to reissue a report after a
    report-code change, and always safe with respect to the shared request
    budget.

    ``force=true`` overrides the quiet-hours guard. Use it knowing the
    price-data service may be fetching at the same time.
    """
    require_admin(key)

    if not skip_fetch and not force and scheduler.in_quiet_hours():
        raise HTTPException(
            status_code=409,
            detail=(
                "Inside the quiet window reserved for the price-data service. "
                "Pass skip_fetch=true to rebuild without touching Yahoo Finance, "
                "or force=true to override."
            ),
        )

    if not scheduler.trigger_build_async(skip_fetch=skip_fetch, force=force):
        raise HTTPException(status_code=409, detail="A build is already running")

    return {"status": "started", "skip_fetch": skip_fetch, "force": force}
