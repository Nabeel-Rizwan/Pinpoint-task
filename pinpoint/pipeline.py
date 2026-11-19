"""The sync pipeline.

One *sync* run = for every configured firm: obtain the raw roster (live with a
fixture fallback), normalise and filter it to corporate/M&A partners, diff it
against the last-known state, persist a new snapshot and write the change log.

The pipeline is the only place that ties the layers together; each collaborator
(adapters, store, diff engine) stays independently testable.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .adapters import AdapterContext, AdapterError, CrawlBudget, get_adapter
from .change import Health, assess_health, diff_roster, health_event
from .httppolite import PoliteFetcher, RobotsDisallowed
from .models import (
    CHANGE_ADDED,
    CHANGE_REMOVED,
    HEALTH_COVERAGE_DROPPED,
    HEALTH_SOURCE_CHANGED,
    HEALTH_SOURCE_DEGRADED,
    HEALTH_TYPES,
    ChangeEvent,
    Firm,
    FirmSyncResult,
    Person,
    SyncResult,
)
from .normalize import (
    canonical_office,
    canonicalize_practices,
    clean_name,
    is_partner,
    matches_corporate_practice,
    normalize_whitespace,
    person_key,
    safe_url,
)
from .storage import Store


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (second precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class _FirmOutcome:
    """Internal accumulator for one firm's ingestion."""

    def __init__(
        self, people: list[Person], fetched: int, source: str, error: str | None,
        notes: list[str], *, incomplete: bool = False, verified_at: str | None = None,
        source_url: str | None = None,
    ) -> None:
        self.people = people          # kept, after partner + corporate/M&A filtering
        self.fetched = fetched        # raw records seen before filtering
        self.source = source
        self.error = error
        self.notes = notes            # adapter diagnostics for this firm
        self.incomplete = incomplete
        self.verified_at = verified_at
        self.source_url = source_url


def sync(
    store: Store,
    firms: list[Firm],
    fetcher: PoliteFetcher,
    *,
    fixtures_dir: Path | str,
    live: bool = False,
    enrich: bool = False,
    fixture_set: str = "current",
    firm_slugs: list[str] | None = None,
    crawl_budget: int | None = None,
    now_iso: str | None = None,
) -> SyncResult:
    """Run a full ingestion + diff cycle and return a summary.

    Args:
        store: persistence façade.
        firms: firms to process (already loaded from config).
        fetcher: polite HTTP client used for any live fetching.
        fixtures_dir: root of the fixture tree (``fixtures/``).
        live: when True, attempt configured real sources and hold failures.
        enrich: when True, fetch per-person profile pages for extra detail.
        fixture_set: which fixture sub-directory to read (``current``/``next``).
        firm_slugs: optional subset of firm slugs to process.
        crawl_budget: total cap on live profile fetches across the whole run
            (``None`` = each adapter's own cap only).
        now_iso: override the run timestamp (used by tests for determinism).
    """
    started = now_iso or utcnow_iso()
    known_slugs = {firm.slug for firm in firms}
    requested = set(firm_slugs or known_slugs)
    if unknown := requested - known_slugs:
        raise ValueError(f"unknown firm(s): {', '.join(sorted(unknown))}")
    selected = [firm for firm in firms if firm.slug in requested]
    if not selected:
        raise ValueError("no firms selected")
    if not live and (not fixture_set or Path(fixture_set).name != fixture_set or fixture_set in (".", "..")):
        raise ValueError("fixture set must be a single directory name")
    budget = CrawlBudget(crawl_budget)

    fixtures_root = Path(fixtures_dir)
    staged = [
        (
            firm, _ingest_firm(
                firm, fetcher, fixtures_root, live=live, enrich=enrich, fixture_set=fixture_set, budget=budget,
            )
        )
        for firm in selected
    ]
    with store.publish():
        return _publish(store, firms, staged, started, live)


def publish_reviewed(store: Store, firms: list[Firm], batches: list, *, now_iso: str | None = None) -> SyncResult:
    """Publish validated, dated observations from reviewed official sources."""
    started = now_iso or utcnow_iso()
    staged = [
        (batch.firm, _FirmOutcome(
            _normalize_roster(batch.firm, batch.people), len(batch.people), "reviewed", None,
            [batch.evidence], source_url=batch.source_url, verified_at=batch.verified_at,
        ))
        for batch in batches
    ]
    if any(len(outcome.people) != outcome.fetched for _, outcome in staged):
        raise ValueError("a reviewed roster contains a duplicate or an out-of-scope person")
    with store.publish():
        for firm, outcome in staged:
            last = store.last_firm_verification(firm.slug)
            if last and outcome.verified_at and outcome.verified_at < last:
                raise ValueError(f"{firm.slug}: reviewed evidence predates the latest verified roster")
        return _publish(store, firms, staged, started, live=True)


def _publish(
    store: Store, firms: list[Firm], staged: list[tuple[Firm, _FirmOutcome]], started: str, live: bool
) -> SyncResult:
    for firm in firms:
        store.upsert_firm(firm)
    source = staged[0][1].source if live else "fixture"
    snapshot_id = store.start_snapshot(started, source)
    all_events: list[ChangeEvent] = []
    results: list[FirmSyncResult] = []

    for firm, outcome in staged:
        previous = store.get_persons(firm.slug)  # last-known-good, active, pre-upsert
        previous_source = store.last_firm_source(firm.slug) or _source_of(previous)
        if not outcome.error:
            _assign_keys(outcome.people, store.get_persons(firm.slug, status=""))
        current = {p.key: p for p in outcome.people}
        if outcome.error:
            status = HEALTH_COVERAGE_DROPPED if outcome.incomplete else HEALTH_SOURCE_DEGRADED
            health = Health(status, outcome.error, len(previous), len(current), previous_source, outcome.source)
        else:
            health = assess_health(previous, previous_source, len(current), outcome.source)

        if health.degraded:
            kept = len(previous)
            events = [health_event(firm.slug, firm.name, health, started, snapshot_id)]
            outcome.notes.append(
                f"{firm.slug}: {health.status} ({health.reason}); held last-known roster"
            )
        else:
            kept = len(current)
            if health.status == HEALTH_SOURCE_CHANGED:
                events = [health_event(firm.slug, firm.name, health, started, snapshot_id)]
            elif live and previous_source is None:
                events = []
            else:
                events = diff_roster(previous, current, started, snapshot_id)
            store.upsert_persons(outcome.people, outcome.verified_at or started)
            store.mark_removed([k for k in previous if k not in current], started)

        all_events.extend(events)
        movement = [e for e in events if e.change_type not in HEALTH_TYPES]
        result = FirmSyncResult(
            firm_slug=firm.slug,
            firm_name=firm.name,
            source=outcome.source,
            fetched=outcome.fetched,
            kept=kept,
            added=sum(1 for e in movement if e.change_type == CHANGE_ADDED),
            removed=sum(1 for e in movement if e.change_type == CHANGE_REMOVED),
            changed=sum(1 for e in movement if e.change_type not in (CHANGE_ADDED, CHANGE_REMOVED)),
            health=health.status,
            health_reason=health.reason,
            verified_at=(outcome.verified_at or started) if live and not health.degraded else None,
            source_url=outcome.source_url or (firm.options.get("practice_url") if live else None),
            error=outcome.error,
            notes=outcome.notes,
        )
        store.record_firm_run(snapshot_id, result, started)
        results.append(result)

    # Persist the immutable point-in-time roster for this snapshot.
    snapshot_people = list(store.get_persons().values())
    store.save_roster(snapshot_id, snapshot_people)
    store.add_events(all_events)

    finished = utcnow_iso()
    store.finish_snapshot(
        snapshot_id,
        finished,
        firm_count=len(staged),
        person_count=len(snapshot_people),
    )

    return SyncResult(snapshot_id=snapshot_id, started_at=started, finished_at=finished, firms=results)


def _source_of(people: dict[str, Person]) -> str | None:
    """The provenance of a firm's stored roster, or ``None`` when it has none.

    Within a firm every record shares a provenance (a run ingests one source),
    so the first record is representative.
    """
    for person in people.values():
        return person.provenance
    return None


def _ingest_firm(
    firm: Firm,
    fetcher: PoliteFetcher,
    fixtures_dir: Path,
    *,
    live: bool,
    enrich: bool,
    fixture_set: str,
    budget: CrawlBudget,
) -> _FirmOutcome:
    """Obtain and normalise one firm's roster; failures never substitute demo data."""
    if not live:
        offline_ctx = AdapterContext(fetcher=fetcher, fixtures_dir=fixtures_dir, fixture_set=fixture_set)
        raw = get_adapter("fixture").load(firm, offline_ctx)
        return _FirmOutcome(_normalize_roster(firm, raw), len(raw), "sample-fixture", None, [])

    live_ctx = AdapterContext(
        fetcher=fetcher,
        fixtures_dir=fixtures_dir,
        fixture_set=fixture_set,
        live=True,
        enrich=enrich,
        budget=budget,
    )
    if gap := firm.options.get("coverage_gap"):
        return _FirmOutcome([], 0, "live", gap, [], incomplete=True)
    adapter = get_adapter(firm.adapter)
    try:
        raw = adapter.load(firm, live_ctx)
        if enrich and raw:
            raw = [adapter.enrich(p, live_ctx) for p in raw]
    except (AdapterError, OSError, RobotsDisallowed, ValueError) as exc:
        return _FirmOutcome([], 0, "live", str(exc), live_ctx.log)

    kept = _normalize_roster(firm, raw)
    if live_ctx.gaps:
        return _FirmOutcome(kept, len(raw), "live", "; ".join(live_ctx.gaps), live_ctx.log, incomplete=True)
    if not kept and not live_ctx.verified_empty:
        return _FirmOutcome([], len(raw), "live", "source returned no qualifying partners", live_ctx.log)
    return _FirmOutcome(kept, len(raw), "live", None, live_ctx.log)


def _normalize_roster(firm: Firm, raw: list[Person]) -> list[Person]:
    """Keep qualifying partners and merge identical observations of one profile."""
    cleaned: dict[tuple, Person] = {}
    for person in raw:
        name = clean_name(person.full_name)
        if not name:
            continue
        if not is_partner(person.title):
            continue
        include = firm.practice_include if person.provenance != "sample-fixture" else ()
        if not matches_corporate_practice(person.practices, include):
            continue

        row = Person(
            key="",
            firm_slug=firm.slug,
            full_name=name,
            title=normalize_whitespace(person.title),
            practices=sorted(set(canonicalize_practices(person.practices) or person.practices)),
            offices=sorted({office for o in person.offices if (office := canonical_office(o))}),
            email=normalize_whitespace(person.email).lower() or None,
            phone=normalize_whitespace(person.phone) or None,
            profile_url=safe_url(person.profile_url),
            provenance=person.provenance,
            source_id=normalize_whitespace(person.source_id) or None,
        )
        base = person_key(firm.slug, name)
        profile = _profile_alias(row.profile_url)
        if row.source_id:
            signature = (row.provenance, "id", row.source_id)
        elif profile:
            signature = (row.provenance, base, profile)
        elif row.email:
            signature = (row.provenance, base, row.email)
        else:
            signature = (row.provenance, base, row.title, tuple(row.practices), tuple(row.offices), row.phone)
        prior = cleaned.get(signature)
        if prior is None:
            cleaned[signature] = row
        else:
            prior.practices = sorted(set(prior.practices) | set(row.practices))
            prior.offices = sorted(set(prior.offices) | set(row.offices))
            prior.email = prior.email or row.email
            prior.phone = prior.phone or row.phone
    return list(cleaned.values())


def _profile_alias(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlsplit(url)
    return urlunsplit(("", parsed.netloc.lower(), parsed.path.rstrip("/"), parsed.query, ""))


def _assign_keys(people: list[Person], history: dict[str, Person]) -> None:
    """Reuse persisted identities, including inactive namesakes, before allocating keys."""
    aliases: dict[tuple, list[str]] = defaultdict(list)

    def evidence(person: Person) -> tuple:
        cohort = person.provenance == "sample-fixture"
        return (
            (cohort, "id", person.source_id),
            (cohort, "url", _profile_alias(person.profile_url)),
            (cohort, "email", person.email),
        )

    def base(person: Person) -> str:
        return person_key(person.firm_slug, person.full_name)

    for key, person in history.items():
        for alias in evidence(person):
            if alias[-1]:
                aliases[alias].append(key)
        aliases[(person.provenance == "sample-fixture", "name", base(person))].append(key)

    observed = Counter(alias for p in people for alias in evidence(p) if alias[-1])
    names = Counter(base(p) for p in people)
    used: set[str] = set()
    for person in people:
        source_id = evidence(person)[0]
        matches = {
            aliases[alias][0]
            for alias in evidence(person)
            if alias[-1] and observed[alias] == 1 and len(aliases[alias]) == 1
        }
        if source_id[-1] and observed[source_id] == 1 and len(aliases[source_id]) == 1:
            matches = {aliases[source_id][0]}
        if len(matches) == 1:
            key = matches.pop()
            if key not in used:
                person.key = key
                used.add(key)

    reserved = set(history)
    for person in sorted(people, key=lambda p: (base(p), p.profile_url or "", p.email or "", p.offices)):
        if person.key:
            continue
        stem = base(person)
        peers = aliases[(person.provenance == "sample-fixture", "name", stem)]
        if names[stem] == 1 and len(peers) == 1 and peers[0] not in used:
            person.key = peers[0]
        else:
            number = 1
            key = stem
            if names[stem] > 1 or peers or key in reserved:
                while f"{stem}#{number}" in reserved:
                    number += 1
                key = f"{stem}#{number}"
            person.key = key
        used.add(person.key)
        reserved.add(person.key)
