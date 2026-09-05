"""Subprocess entrypoint that refreshes trades and builds one report.

Run as::

    python -m api.build_report --out-dir /data/builds/<id> --snapshot-date 2026-09-05

Building in a separate process is deliberate: the builder holds the trade
history and several price series in pandas and forks a chart pool, and a crash
or an OOM here must not be able to take the API down with it. The parent reads
the manifest from stdout and owns all uploads and DB writes.

Progress goes to stderr; stdout carries exactly one machine-readable line.

Note there is no seed-restore step, unlike the price-data service. That
service needs one because twenty years of OHLCV is a 250MB CSV that cannot
live in git. The insider history is ~1.3MB, so it ships in the image and a
cold volume bootstraps from it directly — no R2 round trip, no bulk-download
escape hatch, nothing to keep in sync.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from pathlib import Path

# Emitted on stdout so the parent can find the manifest amid library logging.
MANIFEST_SENTINEL = "__REPORT_MANIFEST__"

logger = logging.getLogger("build_report")


def refresh_trades(full: bool = False) -> dict:
    """Bring the trade store up to date from SEC EDGAR.

    A cold volume is backfilled from SEC's quarterly bulk datasets — roughly
    28,000 qualifying transactions in under half a minute — and then caught
    up to today from the daily filing indexes. A warm volume only does the
    second step.

    There is no seed object and no bootstrap CSV. The price-data service
    needs both because its history is a 250MB file that cannot live in git;
    here the authoritative source rebuilds the whole store faster than
    downloading a seed would take.
    """
    import sec_fetcher
    import store

    store.init_db()
    return sec_fetcher.refresh(full=full)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build one insider trading report.")
    parser.add_argument("--out-dir", required=True, help="Directory to write the PDF into")
    parser.add_argument("--snapshot-date", required=True, help="Report date (YYYY-MM-DD)")
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Build from the trade store as-is, without contacting Yahoo Finance",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Check every ticker, bypassing the adaptive schedule",
    )
    args = parser.parse_args()

    # Progress output contains box-drawing characters. When stdout is a pipe
    # (exactly how the scheduler runs this) Python picks the platform's
    # default codec instead of UTF-8 and those prints raise
    # UnicodeEncodeError. Force UTF-8 on both streams so progress output can
    # never abort a build.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    from api.settings import ensure_dirs

    ensure_dirs()

    try:
        fetch_summary = {}
        if args.skip_fetch:
            logger.info("Skipping trade refresh (--skip-fetch)")
            import store

            store.init_db()
            if store.is_empty():
                # --skip-fetch is for reissuing a report after a code change,
                # so it normally must not touch the network. An empty store
                # is the one case where honouring that would just fail, and
                # a backfill is bulk downloads rather than per-ticker
                # scraping, so it cannot contend with the price service.
                logger.warning(
                    "Store is empty — running a bulk backfill despite --skip-fetch, "
                    "since there is nothing to build a report from otherwise"
                )
                import sec_fetcher

                fetch_summary = sec_fetcher.backfill_from_bulk()
        else:
            fetch_summary = refresh_trades(full=args.full)
            logger.info("Fetch complete: %s", fetch_summary)

        from datetime import datetime, timezone

        from reports.builder import build_report

        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        output_path = out_dir / f"insider_report_{args.snapshot_date}.pdf"

        manifest = build_report(
            output_path=output_path,
            as_of=datetime.now(timezone.utc),
        )
        manifest["fetch"] = fetch_summary
        manifest["snapshot_date"] = args.snapshot_date

    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1

    print(MANIFEST_SENTINEL + json.dumps(manifest), flush=True)
    logger.info("Report build finished: %s", manifest["filename"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
