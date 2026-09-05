#!/usr/bin/env python
"""Command-line entrypoint for the insider trading service.

Everything the hosted service does can be run here against a local ``./data``
directory, so a change can be checked end to end without deploying::

    python main.py fetch        # daily indexes plus a slice of the history gap
    python main.py fetch --full # force a bulk backfill as well
    python main.py report       # build the PDF from the store, no network
    python main.py stats        # what the store holds and how complete it is
    python main.py serve        # run the API on :8003
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # Progress output contains box-drawing characters; the platform default
    # codec raises on them when stdout is redirected.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def cmd_fetch(args: argparse.Namespace) -> int:
    import sec_fetcher
    import store

    store.init_db()
    summary = sec_fetcher.refresh(full=args.full)

    print()
    print("=" * 68)
    for key, value in summary.items():
        print(f"  {key:>18}: {value}")
    print("=" * 68)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    import store
    from reports.builder import build_report

    store.init_db()
    if store.is_empty():
        print("The trade store is empty. Run `python main.py fetch` first.")
        return 1

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = Path(args.out) if args.out else Path(f"insider_report_{date_str}.pdf")

    try:
        manifest = build_report(out, skip_charts=args.skip_charts)
    except PermissionError:
        # The file is often still open in a viewer from the last run.
        stamped = out.with_name(
            f"{out.stem}_{datetime.now(timezone.utc).strftime('%H%M%S')}{out.suffix}"
        )
        print(f"{out.name} is locked — writing {stamped.name} instead")
        manifest = build_report(stamped, skip_charts=args.skip_charts)

    print()
    print("=" * 68)
    for key, value in manifest.items():
        print(f"  {key:>18}: {value}")
    print("=" * 68)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    import store

    store.init_db()
    stats = store.stats()
    coverage = store.index_coverage()

    print()
    print("  TRADE STORE")
    for key, value in stats.items():
        print(f"    {key:>16}: {value}")

    print()
    print("  DAILY INDEX COVERAGE")
    for key, value in coverage.items():
        print(f"    {key:>16}: {value}")

    frame = store.load_trades()
    if not frame.empty:
        print()
        print("  BY TRANSACTION TYPE")
        for kind, count in frame["trade_type"].value_counts().items():
            print(f"    {str(kind):>20}: {count:,}")
    print()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="S&P 500 insider trading briefing, built from SEC EDGAR.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="Update the trade store from SEC EDGAR")
    p_fetch.add_argument(
        "--full",
        action="store_true",
        help="Re-run the bulk quarterly backfill as well as the incremental fetch",
    )
    p_fetch.set_defaults(func=cmd_fetch)

    p_report = sub.add_parser("report", help="Build the PDF briefing")
    p_report.add_argument("--out", help="Output path (default: insider_report_<date>.pdf)")
    p_report.add_argument(
        "--skip-charts",
        action="store_true",
        help="Skip the price download and charts — fastest way to check layout",
    )
    p_report.set_defaults(func=cmd_report)

    p_stats = sub.add_parser("stats", help="Summarise the trade store")
    p_stats.set_defaults(func=cmd_stats)

    p_serve = sub.add_parser("serve", help="Run the API locally")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8003)
    p_serve.add_argument("--reload", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
