"""Daily refresh + report build orchestration.

A single background thread owns the schedule. When a build is due it spawns
``api.build_report`` as a child process, reads the manifest it prints, uploads
the PDF to R2, and flips the report row to ``ready``. The API process itself
never loads the trade history or renders a chart.

Only one build runs at a time — guarded by ``_build_lock`` — so a manual admin
rebuild cannot collide with the scheduled one.

**Yahoo Finance is a shared budget.** The price-data service pulls OHLCV for
the same ~500 tickers daily at 22:00 UTC. This service builds at 11:00 UTC,
which keeps the scheduled runs eleven hours apart, and additionally refuses to
*start* a fetching build during ``QUIET_HOURS_UTC``. The quiet window is what
covers the cases a build hour alone does not: a redeploy firing BUILD_ON_BOOT
at an arbitrary time, an admin rebuild, or a run that overruns.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from api import database
from api.build_report import MANIFEST_SENTINEL
from api.settings import (
    BUILD_HOUR_UTC,
    BUILD_ON_BOOT,
    BUILD_TIMEOUT_SECONDS,
    BUILD_WORK_DIR,
    FETCH_FULL_UNIVERSE,
    SCHEDULER_POLL_SECONDS,
    ensure_dirs,
    quiet_hours,
)
from api.storage import upload_report_file
from config import REPO_ROOT

logger = logging.getLogger(__name__)

# What one build produces. A single artifact today, but kept as a keyed
# catalogue so adding a CSV or an editable copy later is a manifest change
# rather than a schema migration.
REPORT_SLUG = "insider"
REPORT_TITLE = "Insider Trading Briefing"
REPORT_DESCRIPTION = (
    "Every S&P 500 insider transaction of $1M or more, filtered to genuine "
    "open-market purchases and ranked by conviction. Leads with multi-insider "
    "purchase clusters — several executives buying the same stock on the same "
    "day — each scored on participant count, dollar size, direct ownership and "
    "seniority, and tracked against the S&P 500 since the purchase date."
)

_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_build_lock = threading.Lock()

# Set when a boot build is deferred by the quiet window, so the scheduler
# retries it as soon as the window clears rather than waiting for the next
# build hour.
_deferred_build = threading.Event()

_state: Dict[str, Any] = {
    "building": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_report_id": None,
    "last_error": None,
    "last_fetch": None,
    "deferred_by_quiet_hours": False,
}
_state_lock = threading.Lock()


def _set_state(**kwargs: Any) -> None:
    with _state_lock:
        _state.update(kwargs)


def get_state() -> Dict[str, Any]:
    with _state_lock:
        state = dict(_state)
    state["build_hour_utc"] = BUILD_HOUR_UTC
    state["quiet_hours_utc"] = sorted(quiet_hours())
    return state


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> str:
    return _now().strftime("%Y-%m-%d")


def in_quiet_hours(now: Optional[datetime] = None) -> bool:
    """True while the price-data service may be using the Yahoo budget."""
    now = now or _now()
    return now.hour in quiet_hours()


# ── Child process ────────────────────────────────────────────────────────────


def _drain(stream, sink: deque) -> None:
    """Log the child's stderr live and keep a tail for error reporting."""
    for line in iter(stream.readline, ""):
        line = line.rstrip()
        if line:
            sink.append(line)
            logger.info("[build] %s", line)
    stream.close()


def _run_build_subprocess(
    out_dir: Path, snapshot_date: str, skip_fetch: bool
) -> Dict[str, Any]:
    """Run api.build_report and return its manifest.

    Raises RuntimeError with the tail of the child's stderr on failure.
    """
    cmd = [
        sys.executable,
        "-m",
        "api.build_report",
        "--out-dir",
        str(out_dir),
        "--snapshot-date",
        snapshot_date,
    ]
    if skip_fetch:
        cmd.append("--skip-fetch")
    if FETCH_FULL_UNIVERSE:
        cmd.append("--full")

    logger.info("Spawning build: %s", " ".join(cmd))
    # UTF-8 is pinned on both sides of the pipe: progress output contains
    # box-drawing characters and the default locale codec would raise on
    # either encode or decode. errors="replace" keeps a stray byte from
    # killing a good build.
    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    err_tail: deque = deque(maxlen=40)
    drainer = threading.Thread(target=_drain, args=(proc.stderr, err_tail), daemon=True)
    drainer.start()

    # Watchdog: a wedged network call must not block every future build.
    timed_out = threading.Event()

    def _kill() -> None:
        timed_out.set()
        logger.error("Build exceeded %ss — killing", BUILD_TIMEOUT_SECONDS)
        proc.kill()

    watchdog = threading.Timer(BUILD_TIMEOUT_SECONDS, _kill)
    watchdog.start()

    try:
        stdout_data = proc.stdout.read()
        returncode = proc.wait()
    finally:
        watchdog.cancel()
        proc.stdout.close()
        drainer.join(timeout=10)

    if timed_out.is_set():
        raise RuntimeError(f"Build timed out after {BUILD_TIMEOUT_SECONDS}s")

    if returncode != 0:
        raise RuntimeError(
            f"Build process exited {returncode}. Last output:\n" + "\n".join(err_tail)
        )

    for line in stdout_data.splitlines():
        if line.startswith(MANIFEST_SENTINEL):
            return json.loads(line[len(MANIFEST_SENTINEL):])

    raise RuntimeError("Build produced no manifest. Last output:\n" + "\n".join(err_tail))


# ── Build orchestration ──────────────────────────────────────────────────────


def run_build(skip_fetch: bool = False, force: bool = False) -> Optional[str]:
    """Build, upload, and register one report.

    Returns the report id, or None if another build is running or the quiet
    window deferred this one.

    The quiet-hours check happens *before* the report row is created. A skip
    path that returned after creating the row would leave it stuck in
    ``building``, and ``has_report_for_date`` would then suppress every retry
    for the rest of the day.
    """
    if not _build_lock.acquire(blocking=False):
        logger.info("Build already in progress — skipping this trigger")
        return None

    try:
        if not skip_fetch and not force and in_quiet_hours():
            logger.info(
                "Deferring build: %02d:00 UTC is inside the quiet window %s, when "
                "the price-data service may be using the shared Yahoo budget",
                _now().hour, sorted(quiet_hours()),
            )
            _deferred_build.set()
            _set_state(deferred_by_quiet_hours=True)
            return None

        _deferred_build.clear()
        _set_state(deferred_by_quiet_hours=False)

        return _build_locked(skip_fetch)
    finally:
        _build_lock.release()


def _build_locked(skip_fetch: bool) -> Optional[str]:
    """The build itself. Called only with ``_build_lock`` held."""
    ensure_dirs()
    snapshot_date = _today()
    report_id = database.create_report(snapshot_date)
    out_dir = BUILD_WORK_DIR / report_id

    _set_state(
        building=True,
        last_started_at=_now().isoformat(),
        last_report_id=report_id,
        last_error=None,
    )
    logger.info("Starting report %s for %s", report_id, snapshot_date)

    try:
        manifest = _run_build_subprocess(out_dir, snapshot_date, skip_fetch)

        path = Path(manifest["path"])
        key = upload_report_file(snapshot_date, report_id, path)
        report_keys = {
            REPORT_SLUG: {
                "key": key,
                "filename": manifest["filename"],
                "title": REPORT_TITLE,
                "description": REPORT_DESCRIPTION,
                "bytes": manifest["bytes"],
                "kind": "PDF",
            }
        }
        logger.info("Uploaded %s → %s", manifest["filename"], key)

        database.mark_report_ready(report_id, report_keys, manifest)
        _set_state(
            building=False,
            last_finished_at=_now().isoformat(),
            last_error=None,
            last_fetch=manifest.get("fetch"),
        )
        logger.info(
            "Report %s ready: %s clusters, %s purchases in window",
            report_id, manifest.get("cluster_count"), manifest.get("purchase_count"),
        )
        return report_id

    except Exception as exc:
        logger.exception("Report %s failed", report_id)
        database.mark_report_failed(report_id, str(exc))
        _set_state(building=False, last_finished_at=_now().isoformat(), last_error=str(exc))
        return None

    finally:
        _cleanup_work_dir(out_dir)


def _cleanup_work_dir(out_dir: Path) -> None:
    """Drop the local PDF once it is safely in R2."""
    if not out_dir.exists():
        return
    try:
        for item in out_dir.iterdir():
            item.unlink()
        out_dir.rmdir()
    except OSError as exc:
        logger.warning("Could not clean build dir %s: %s", out_dir, exc)


# ── Schedule ─────────────────────────────────────────────────────────────────


def _build_is_due() -> bool:
    """True when today's report has not been built and the hour has passed."""
    if _now().hour < BUILD_HOUR_UTC:
        return False
    return not database.has_report_for_date(_today())


def _loop() -> None:
    # A report left "building" cannot resume after a restart, and would
    # otherwise make has_report_for_date() block today's real build.
    reaped = database.reap_stale_building_reports()
    if reaped:
        logger.warning("Marked %s interrupted report(s) as failed", reaped)

    if BUILD_ON_BOOT and database.get_latest_ready_report() is None:
        logger.info("No ready report exists — building one now")
        run_build()

    while not _stop_event.is_set():
        try:
            # A build the quiet window turned away is retried the moment the
            # window clears, rather than waiting for tomorrow's build hour.
            if _deferred_build.is_set() and not in_quiet_hours():
                logger.info("Quiet window has cleared — retrying the deferred build")
                run_build()
            elif _build_is_due():
                logger.info("Daily build is due")
                run_build()
        except Exception:
            logger.exception("Scheduler loop error")
        _stop_event.wait(timeout=SCHEDULER_POLL_SECONDS)


def start_scheduler() -> None:
    """Start the background scheduler thread (idempotent)."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="report-scheduler")
    _thread.start()
    logger.info(
        "Scheduler started (daily build at %02d:00 UTC, quiet hours %s)",
        BUILD_HOUR_UTC, sorted(quiet_hours()),
    )


def stop_scheduler() -> None:
    """Signal the scheduler to stop and wait briefly for it."""
    _stop_event.set()
    if _thread:
        _thread.join(timeout=10)
    logger.info("Scheduler stopped")


def trigger_build_async(skip_fetch: bool = False, force: bool = False) -> bool:
    """Kick a build off-thread. False if one is already running."""
    if _build_lock.locked():
        return False
    threading.Thread(
        target=run_build,
        kwargs={"skip_fetch": skip_fetch, "force": force},
        daemon=True,
        name="manual-build",
    ).start()
    return True
