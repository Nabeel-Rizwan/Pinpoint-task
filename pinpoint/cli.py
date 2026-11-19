"""Command-line interface.

Subcommands mirror the product lifecycle: ``init`` (provision), ``sync``
(refresh), ``changes``/``search`` (query), ``report``/``serve`` (surface), plus
``demo`` (a one-command offline walkthrough) and ``doctor`` (environment check).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

try:  # POSIX advisory locks; a no-op on platforms without fcntl.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from .config import PROJECT_ROOT, load_firms, load_settings
from .httppolite import PoliteFetcher
from .models import HEALTH_OK, SyncResult
from .pipeline import publish_reviewed, sync, utcnow_iso
from .report import build_report
from .reviewed import load_reviewed
from .server import serve
from .storage import Store

DEFAULT_DB = PROJECT_ROOT / "data" / "roster.db"
DEFAULT_OUT = PROJECT_ROOT / "out"
DEMO_DB = PROJECT_ROOT / "data" / "demo" / "roster.db"
DEMO_OUT = PROJECT_ROOT / "out" / "demo"
FIXTURES_DIR = PROJECT_ROOT / "fixtures"
CACHE_DIR = PROJECT_ROOT / "data" / "cache"

# Change-log types accepted by ``pinpoint changes --type``.
CHANGE_TYPES = [
    "ADDED",
    "REMOVED",
    "TITLE_CHANGED",
    "PRACTICE_CHANGED",
    "OFFICE_CHANGED",
    "CONTACT_UPDATED",
    "PROFILE_UPDATED",
    "SOURCE_DEGRADED",
    "SOURCE_CHANGED",
    "COVERAGE_DROPPED",
]


@contextlib.contextmanager
def _sync_lock(path: Path):
    """Refuse to start a second sync while one is already running.

    A weekly cron plus a slow crawl can overlap; two writers on one SQLite file
    would interleave snapshots. The lock is advisory (``flock``) and released
    automatically if the process dies.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError(f"another sync appears to be running (lock: {path})") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        handle.close()


def _open_store(db_path: Path) -> Store:
    """Open (and migrate) the store at ``db_path``."""
    return Store(db_path)


def _make_fetcher(*, live: bool) -> PoliteFetcher:
    """Build a fetcher; offline when not hitting the network."""
    settings = load_settings()
    if live:
        user_agent = os.environ.get("PINPOINT_USER_AGENT", settings.user_agent).strip()
        if not user_agent or not settings.respect_robots or not settings.ssl_verify:
            raise ValueError("live refreshes need an identifiable user agent, robots checks, and HTTPS verification")
        settings = replace(settings, user_agent=user_agent, cache_ttl_seconds=0)
    return PoliteFetcher(settings, CACHE_DIR, offline=not live)


def _print_sync_summary(result: SyncResult) -> None:
    """Pretty-print the outcome of a sync run."""
    print(f"\nSync #{result.snapshot_id}  ({result.firms[0].source})  {result.started_at}")
    print(
        f"{'Firm':32} {'Source':16} {'Health':16} {'Seen':>5} {'Kept':>5} "
        f"{'+Add':>5} {'-Del':>5} {'~Chg':>5}  Note"
    )
    print("-" * 122)
    for f in sorted(result.firms, key=lambda x: x.firm_name):
        health = "" if f.health == HEALTH_OK else f.health
        note = (f.error or "")[:40]
        print(
            f"{f.firm_name[:31]:32} {f.source:16} {health:16} {f.fetched:>5} {f.kept:>5} {f.added:>5} "
            f"{f.removed:>5} {f.changed:>5}  {note}"
        )
    print("-" * 122)
    print(
        f"{'TOTAL':32} {'':16} {'':16} {'':>5} {result.total_kept:>5} {'':>5} {'':>5} "
        f"{result.total_changes:>5}"
    )


def _print_notes(result: SyncResult) -> None:
    """Print the per-firm adapter diagnostics collected during a run."""
    any_notes = False
    for f in sorted(result.firms, key=lambda x: x.firm_name):
        if not f.notes:
            continue
        if not any_notes:
            print("\nAdapter diagnostics:")
            any_notes = True
        for note in f.notes:
            print(f"  {note}")
    if not any_notes:
        print("\nAdapter diagnostics: (none recorded)")


# --------------------------------------------------------------------- commands
def cmd_init(args: argparse.Namespace) -> int:
    """Provision the database and register the configured firms."""
    db_path = Path(args.db)
    firms = load_firms()
    with _sync_lock(db_path.with_suffix(".lock")), contextlib.closing(_open_store(db_path)) as store:
        with store.publish():
            for firm in firms:
                store.upsert_firm(firm)
    print(f"Initialised {db_path} with {len(firms)} firms.")
    return 0


def cmd_firms(args: argparse.Namespace) -> int:
    """List the firms configured for ingestion."""
    firms = load_firms()
    print(f"{'Slug':16} {'Adapter':12} {'Name':45} Partners URL")
    print("-" * 120)
    for f in firms:
        print(f"{f.slug:16} {f.adapter:12} {f.name[:44]:45} {f.partners_url}")
    return 0


def _report_degradation(result: SyncResult) -> int:
    """Report firms that could not be refreshed and return a run exit code.

    A scheduled run must fail visibly. A week where firms silently fell back to
    the sample snapshot is a *coverage* failure, not an empty roster, so we exit
    non-zero and name the firms rather than pretending the run succeeded.
    """
    degraded = result.degraded_firms
    if not degraded:
        return 0
    print(f"\n{len(degraded)}/{len(result.firms)} firm(s) could not be verified.", file=sys.stderr)
    for f in degraded:
        print(f"  {f.firm_name}: {f.health} ({f.health_reason})", file=sys.stderr)
    return 1


def cmd_sync(args: argparse.Namespace) -> int:
    """Refresh from official sources or explicitly selected synthetic fixtures."""
    firms = load_firms()
    settings = load_settings()
    live = args.fixture_set is None
    db_path = Path(args.db or (DEFAULT_DB if live else DEMO_DB)).resolve()
    if live and db_path == DEMO_DB.resolve():
        raise ValueError("live refreshes require a production database")
    if not live and db_path == DEFAULT_DB.resolve():
        raise ValueError("the production database cannot receive synthetic fixtures")
    if args.firm and (unknown := set(args.firm) - {firm.slug for firm in firms}):
        raise ValueError(f"unknown firm(s): {', '.join(sorted(unknown))}")
    with _sync_lock(db_path.with_suffix(".lock")), contextlib.closing(_open_store(db_path)) as store:
        _check_database_source(store, live=live)
        result = sync(
            store, firms, _make_fetcher(live=live), fixtures_dir=FIXTURES_DIR, live=live,
            enrich=args.enrich, fixture_set=args.fixture_set or "current", firm_slugs=args.firm,
            crawl_budget=settings.max_profiles_total,
        )
    if args.json:
        print(json.dumps(asdict(result), ensure_ascii=False))
    else:
        _print_sync_summary(result)
        if args.verbose:
            _print_notes(result)
    return _report_degradation(result)


def _check_database_source(store: Store, *, live: bool) -> None:
    for snapshot in store.list_snapshots(limit=1):
        if (snapshot["source"] == "fixture") == live:
            raise ValueError("choose a separate database for real and synthetic rosters")


def cmd_import_roster(args: argparse.Namespace) -> int:
    """Validate and import a dated roster without contacting third-party sites."""
    db_path = Path(args.db).resolve()
    if db_path == DEMO_DB.resolve():
        raise ValueError("reviewed rosters require a production database")
    firms = load_firms()
    batches = load_reviewed(Path(args.manifest), firms)
    with _sync_lock(db_path.with_suffix(".lock")), contextlib.closing(_open_store(db_path)) as store:
        _check_database_source(store, live=True)
        result = publish_reviewed(store, firms, batches)
    _print_sync_summary(result)
    return _report_degradation(result)


def cmd_report(args: argparse.Namespace) -> int:
    """Generate the dashboard and CSV exports."""
    store = _open_store(Path(args.db))
    firms = load_firms()
    paths = build_report(store, firms, Path(args.out), utcnow_iso())
    print("Wrote:")
    for name, path in paths.items():
        print(f"  {name:14} {path}")
    print("\nThe dashboard is API-driven — serve it: python3 -m pinpoint serve")
    store.close()
    return 0


def cmd_changes(args: argparse.Namespace) -> int:
    """Print the recent change log."""
    store = _open_store(Path(args.db))
    firms = load_firms()
    names = {f.slug: f.name for f in firms}
    changes = store.recent_changes(firm_slug=args.firm, change_type=args.type, limit=args.limit)
    if not changes:
        print("No changes recorded.")
        store.close()
        return 0
    print(f"{'When':20} {'Type':16} {'Partner':28} {'Firm':28} Change")
    print("-" * 130)
    for c in changes:
        arrow = ""
        if c.get("field"):
            arrow = f'{c["field"]}: {c.get("old_value") or "-"} -> {c.get("new_value") or "-"}'
        print(
            f"{c['detected_at'][:19]:20} {c['change_type']:16} "
            f"{c['full_name'][:27]:28} {names.get(c['firm_slug'], c['firm_slug'])[:27]:28} {arrow}"
        )
    store.close()
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """Filter the stored partner list."""
    store = _open_store(Path(args.db))
    firms = load_firms()
    names = {f.slug: f.name for f in firms}
    people = store.list_persons(
        firm_slug=args.firm, practice=args.practice, office=args.office, search=args.query
    )
    print(f"{len(people)} partner(s) matched.\n")
    for p in people:
        print(
            f"{p.full_name}  |  {names.get(p.firm_slug, p.firm_slug)}  |  {p.title}  |  "
            f"{', '.join(p.practices) or '-'}  |  {', '.join(p.offices) or '-'}"
        )
    store.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Build (optionally) and serve the dashboard + JSON API."""
    db_path = Path(args.db or (DEMO_DB if args.demo else DEFAULT_DB))
    directory = Path(args.out or (DEMO_OUT if args.demo else DEFAULT_OUT))
    if not db_path.is_file():
        raise ValueError(f"database does not exist: {db_path}; run demo or sync first")
    with contextlib.closing(_open_store(db_path)) as store:
        firms = load_firms()
        if args.build or not (directory / "index.html").exists():
            build_report(store, firms, directory, utcnow_iso())
        serve(store, firms, directory, host=args.host, port=args.port)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check the environment, config and (optionally) network reachability."""
    print(f"Python:        {sys.version.split()[0]}")
    print(f"Project root:  {PROJECT_ROOT}")
    print(f"Database:      {DEFAULT_DB} ({'exists' if DEFAULT_DB.exists() else 'not created yet'})")

    firms = load_firms()
    print(f"Firms loaded:  {len(firms)}")
    settings = load_settings()
    print(f"User-Agent:    {settings.user_agent}")
    print(f"Respect robots:{settings.respect_robots}   min delay: {settings.min_delay_seconds}s")
    cap = settings.max_profiles_total
    print(f"Run budget:    {'unbounded' if cap is None else cap} profile fetch(es) per run")

    missing = [f.slug for f in firms if not (FIXTURES_DIR / "current" / f"{f.slug}.json").exists()]
    print(f"Demo fixtures: {len(firms) - len(missing)}/{len(firms)} firms have a baseline fixture")
    if missing:
        print(f"  missing fixtures: {', '.join(missing)}")
    no_offices = [f.slug for f in firms if not f.options.get("offices")]
    if no_offices:
        print(f"  firms with no offices configured: {', '.join(no_offices)}")

    if args.check_network:
        fetcher = _make_fetcher(live=True)
        print("\nRobots/allowance check (first 5 firms):")
        for firm in firms[:5]:
            try:
                allowed = fetcher.is_allowed(firm.partners_url)
            except Exception as exc:  # pragma: no cover - network dependent
                allowed = f"error: {exc}"
            print(f"  {firm.slug:16} allowed={allowed}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Initialize the separate, offline demo database and dashboard."""
    firms = load_firms()
    with _sync_lock(DEMO_DB.with_suffix(".lock")), contextlib.closing(_open_store(DEMO_DB)) as store:
        snapshots = store.list_snapshots(limit=2)
        if len(snapshots) < 2:
            fetcher = _make_fetcher(live=False)
            dates = ("2025-01-06T06:00:00+00:00", "2025-01-13T06:00:00+00:00")
            for index, fixture_set in enumerate(("current", "next")):
                if index < len(snapshots):
                    continue
                result = sync(
                    store, firms, fetcher, fixtures_dir=FIXTURES_DIR,
                    fixture_set=fixture_set, now_iso=dates[index],
                )
                _print_sync_summary(result)
        paths = build_report(store, firms, DEMO_OUT, utcnow_iso())
    print(f"Demo dashboard written to {paths['index']}")
    print("Run python3 -m pinpoint serve --demo and open http://127.0.0.1:8000/")
    return 0


def cmd_snapshots(args: argparse.Namespace) -> int:
    """List ingestion snapshots (the point-in-time history)."""
    store = _open_store(Path(args.db))
    rows = store.list_snapshots(limit=args.limit)
    if not rows:
        print("No snapshots recorded yet. Run: python3 -m pinpoint sync")
        store.close()
        return 0
    print(f"{'ID':>4}  {'Started':19}  {'Finished':19}  {'Source':10} {'Firms':>5} {'People':>6}")
    print("-" * 74)
    for r in rows:
        print(
            f"{r['id']:>4}  {(r['started_at'] or '')[:19]:19}  {(r['finished_at'] or '')[:19]:19}  "
            f"{(r['source'] or ''):10} {r['firm_count'] or 0:>5} {r['person_count'] or 0:>6}"
        )
    store.close()
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    """Show the partner roster as it was recorded at a given snapshot."""
    store = _open_store(Path(args.db))
    firms = load_firms()
    names = {f.slug: f.name for f in firms}

    if args.snapshot is not None:
        snapshot_id = args.snapshot
    elif args.at:
        at = datetime.fromisoformat(args.at.replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        snapshot_id = store.snapshot_id_at_or_before(at.isoformat())
    else:
        snapshot_id = store.latest_snapshot_id()

    if snapshot_id is None:
        print("No matching snapshot. Run: python3 -m pinpoint sync")
        store.close()
        return 0

    people = store.roster_at(snapshot_id)
    if args.firm:
        people = [p for p in people if p.firm_slug == args.firm]
    print(f"Snapshot #{snapshot_id}: {len(people)} partner(s)\n")
    for p in sorted(people, key=lambda x: (x.firm_slug, x.full_name)):
        print(
            f"{p.full_name}  |  {names.get(p.firm_slug, p.firm_slug)}  |  {p.title}  |  "
            f"{', '.join(p.practices) or '-'}  |  {', '.join(p.offices) or '-'}"
        )
    store.close()
    return 0


# --------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for all subcommands."""
    parser = argparse.ArgumentParser(
        prog="pinpoint",
        description="Corporate & M&A partner intelligence for recruiters.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create the database and register firms")
    p_init.add_argument("--db", default=str(DEFAULT_DB))
    p_init.set_defaults(func=cmd_init)

    p_firms = sub.add_parser("firms", help="list configured firms")
    p_firms.set_defaults(func=cmd_firms)

    p_sync = sub.add_parser("sync", help="refresh + diff the partner data")
    p_sync.add_argument("--db")
    source = p_sync.add_mutually_exclusive_group()
    source.add_argument("--live", action="store_true", help="refresh from official sources (default)")
    source.add_argument("--fixture-set", help="use a synthetic set such as current or next")
    p_sync.add_argument("--enrich", action="store_true", help="fetch per-person profile pages")
    p_sync.add_argument("--firm", action="append", help="limit to a firm slug (repeatable)")
    p_sync.add_argument("--verbose", action="store_true", help="print adapter diagnostics")
    p_sync.add_argument("--json", action="store_true", help="write a machine-readable run summary")
    p_sync.set_defaults(func=cmd_sync)

    p_import = sub.add_parser("import-roster", help="import a reviewed official-source manifest")
    p_import.add_argument("manifest")
    p_import.add_argument("--db", default=str(DEFAULT_DB))
    p_import.set_defaults(func=cmd_import_roster)

    p_report = sub.add_parser("report", help="generate the dashboard + CSVs")
    p_report.add_argument("--db", default=str(DEFAULT_DB))
    p_report.add_argument("--out", default=str(DEFAULT_OUT))
    p_report.set_defaults(func=cmd_report)

    p_changes = sub.add_parser("changes", help="show the recent change log")
    p_changes.add_argument("--db", default=str(DEFAULT_DB))
    p_changes.add_argument("--firm")
    p_changes.add_argument("--type", choices=CHANGE_TYPES)
    p_changes.add_argument("--limit", type=int, default=50)
    p_changes.set_defaults(func=cmd_changes)

    p_search = sub.add_parser("search", help="filter the stored partner list")
    p_search.add_argument("--db", default=str(DEFAULT_DB))
    p_search.add_argument("--firm")
    p_search.add_argument("--practice")
    p_search.add_argument("--office")
    p_search.add_argument("-q", "--query", help="name substring")
    p_search.set_defaults(func=cmd_search)

    p_snapshots = sub.add_parser("snapshots", help="list ingestion snapshots")
    p_snapshots.add_argument("--db", default=str(DEFAULT_DB))
    p_snapshots.add_argument("--limit", type=int, default=50)
    p_snapshots.set_defaults(func=cmd_snapshots)

    p_history = sub.add_parser("history", help="show the roster as of a past snapshot")
    p_history.add_argument("--db", default=str(DEFAULT_DB))
    p_history.add_argument("--snapshot", type=int, help="explicit snapshot id")
    p_history.add_argument("--at", help="ISO timestamp; newest snapshot at or before it")
    p_history.add_argument("--firm", help="limit to a firm slug")
    p_history.set_defaults(func=cmd_history)

    p_serve = sub.add_parser("serve", help="serve the dashboard + JSON API")
    p_serve.add_argument("--db")
    p_serve.add_argument("--out")
    p_serve.add_argument("--demo", action="store_true", help="serve the separate synthetic demo")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--build", action="store_true", help="regenerate the report first")
    p_serve.set_defaults(func=cmd_serve)

    p_doctor = sub.add_parser("doctor", help="check environment/config")
    p_doctor.add_argument("--check-network", action="store_true", help="probe robots.txt for a few firms")
    p_doctor.set_defaults(func=cmd_doctor)

    p_demo = sub.add_parser("demo", help="offline end-to-end walkthrough")
    p_demo.set_defaults(func=cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"pinpoint: {exc}", file=sys.stderr)
        return 1
