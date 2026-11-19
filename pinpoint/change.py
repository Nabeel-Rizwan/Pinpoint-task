"""Change detection.

Given the previous known state of a firm's roster and the freshly-observed one,
produce the list of change events a recruiter cares about: who joined, who left,
who was promoted into/out of partner, who moved office, and whose practice mix
changed.

Pure functions only — no I/O — so the semantics are easy to reason about and to
test. The pipeline calls :func:`diff_roster` once per firm per run.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    CHANGE_ADDED,
    CHANGE_CONTACT,
    CHANGE_COVERAGE_DROPPED,
    CHANGE_OFFICE,
    CHANGE_PRACTICE,
    CHANGE_PROFILE,
    CHANGE_REMOVED,
    CHANGE_SOURCE_CHANGED,
    CHANGE_SOURCE_DEGRADED,
    CHANGE_TITLE,
    DEGRADED_HEALTH,
    HEALTH_COVERAGE_DROPPED,
    HEALTH_OK,
    HEALTH_SOURCE_CHANGED,
    HEALTH_SOURCE_DEGRADED,
    ChangeEvent,
    Person,
)
from .normalize import name_key

#: A live parse that returns less than this fraction of the previous roster is
#: treated as a broken/partial parse rather than genuine attrition.
COVERAGE_FLOOR = 0.5
#: ...but only when the previous roster was at least this big. A firm going from
#: 2 partners to 1 is not necessarily broken; one going from 30 to 4 is.
COVERAGE_MIN_BASELINE = 5

_HEALTH_CHANGE_TYPE = {
    HEALTH_SOURCE_DEGRADED: CHANGE_SOURCE_DEGRADED,
    HEALTH_COVERAGE_DROPPED: CHANGE_COVERAGE_DROPPED,
    HEALTH_SOURCE_CHANGED: CHANGE_SOURCE_CHANGED,
}


def _join(values: list[str]) -> str:
    """Render a list for storage in a single text column."""
    return "; ".join(values)


@dataclass(frozen=True)
class Health:
    """A firm's ingestion health for one run, versus its last-known state."""

    status: str = HEALTH_OK
    reason: str = ""
    previous_count: int = 0
    current_count: int = 0
    previous_source: str | None = None
    current_source: str = ""

    @property
    def degraded(self) -> bool:
        """True when the firm could not be refreshed from a trusted source."""
        return self.status in DEGRADED_HEALTH


def assess_health(
    previous: dict[str, Person],
    previous_source: str | None,
    current_count: int,
    current_source: str,
) -> Health:
    """Classify a firm's ingestion this run against its last-known-good state.

    Two failure modes are caught that a plain diff would mistake for *movement*:

    * a **source change** — most often live -> fixture, i.e. the site broke and we
      fell back to the sample snapshot. Diffing across sources compares two
      different populations and fabricates joins/leaves.
    * a **coverage collapse** — a live parse that silently returns a fraction of
      last week's roster (selector drift, pagination) reads as mass attrition.
    """
    prev_count = len(previous)
    if previous_source is None:
        return Health(HEALTH_OK, "", prev_count, current_count, previous_source, current_source)

    if current_source != previous_source:
        status = (
            HEALTH_SOURCE_DEGRADED if current_source == "sample-fixture" else HEALTH_SOURCE_CHANGED
        )
        return Health(
            status,
            f"source {previous_source} -> {current_source}",
            prev_count,
            current_count,
            previous_source,
            current_source,
        )

    if prev_count >= COVERAGE_MIN_BASELINE and current_count < prev_count * COVERAGE_FLOOR:
        return Health(
            HEALTH_COVERAGE_DROPPED,
            f"coverage {prev_count} -> {current_count}",
            prev_count,
            current_count,
            previous_source,
            current_source,
        )

    return Health(HEALTH_OK, "", prev_count, current_count, previous_source, current_source)


def firm_status(row: dict, flag: str | None) -> str:
    """Classify a firm's freshness for the dashboard: a short status label.

    ``stale`` means the latest run could not refresh the firm and is holding
    last-known data; ``sample`` means every record is a fixture; ``no data``
    means the firm has no active partners; otherwise ``live``.
    """
    if row.get("health") in DEGRADED_HEALTH or (
        not row.get("last_attempt") and flag in (CHANGE_SOURCE_DEGRADED, CHANGE_COVERAGE_DROPPED)
    ):
        return "stale"
    if not row.get("partners") and row.get("last_verified"):
        return "verified empty"
    if not row.get("partners") and row.get("coverage_gap"):
        return "coverage gap"
    if not row.get("last_attempt") and row.get("live"):
        return "unverified"
    if not row.get("partners"):
        return "no data"
    if not row.get("live") and row.get("reviewed"):
        return "reviewed"
    if not row.get("live"):
        return "sample"
    return "live"


def health_event(
    firm_slug: str,
    firm_name: str,
    health: Health,
    detected_at: str,
    snapshot_id: int | None = None,
) -> ChangeEvent:
    """Build the single, visible firm-level event for a health outcome.

    ``person_key`` uses a ``slug::(firm)`` sentinel so the row is clearly about a
    firm, not a person, and never collides with a real ``person_key``.
    """
    if health.status == HEALTH_COVERAGE_DROPPED:
        old_value, new_value = str(health.previous_count), str(health.current_count)
    else:
        old_value, new_value = health.previous_source, health.current_source
    return ChangeEvent(
        person_key=f"{firm_slug}::(firm)",
        firm_slug=firm_slug,
        full_name=firm_name,
        change_type=_HEALTH_CHANGE_TYPE[health.status],
        field="firm",
        old_value=old_value,
        new_value=new_value,
        detected_at=detected_at,
        snapshot_id=snapshot_id,
    )


def diff_roster(
    previous: dict[str, Person],
    current: dict[str, Person],
    detected_at: str,
    snapshot_id: int | None = None,
) -> list[ChangeEvent]:
    """Diff two roster snapshots keyed by ``person_key``.

    ``previous`` is the last-known state (from storage); ``current`` is what we
    just observed. Field-level changes are only emitted for people present in
    both, so a person who left and re-joined reads as REMOVED then ADDED rather
    than a burst of field-level noise.
    """
    events: list[ChangeEvent] = []

    for key, person in current.items():
        if key not in previous:
            events.append(
                ChangeEvent(
                    person_key=key,
                    firm_slug=person.firm_slug,
                    full_name=person.full_name,
                    change_type=CHANGE_ADDED,
                    new_value=person.title or None,
                    detected_at=detected_at,
                    snapshot_id=snapshot_id,
                )
            )
            continue

        prev = previous[key]
        events.extend(_field_changes(prev, person, detected_at, snapshot_id))

    for key, person in previous.items():
        if key not in current:
            events.append(
                ChangeEvent(
                    person_key=key,
                    firm_slug=person.firm_slug,
                    full_name=person.full_name,
                    change_type=CHANGE_REMOVED,
                    old_value=person.title or None,
                    detected_at=detected_at,
                    snapshot_id=snapshot_id,
                )
            )

    # Deterministic ordering keeps diffs stable for tests and diffs-of-diffs.
    events.sort(key=lambda e: (e.firm_slug, e.change_type, e.full_name))
    return events


def _field_changes(
    prev: Person, curr: Person, detected_at: str, snapshot_id: int | None
) -> list[ChangeEvent]:
    """Emit field-level events for a person present in both snapshots."""
    events: list[ChangeEvent] = []

    def emit(change_type: str, field: str, old: str | None, new: str | None) -> None:
        events.append(
            ChangeEvent(
                person_key=curr.key,
                firm_slug=curr.firm_slug,
                full_name=curr.full_name,
                change_type=change_type,
                field=field,
                old_value=old,
                new_value=new,
                detected_at=detected_at,
                snapshot_id=snapshot_id,
            )
        )

    if prev.title != curr.title:
        emit(CHANGE_TITLE, "title", prev.title or None, curr.title or None)

    if sorted(prev.practices) != sorted(curr.practices):
        emit(CHANGE_PRACTICE, "practices", _join(sorted(prev.practices)), _join(sorted(curr.practices)))

    if sorted(prev.offices) != sorted(curr.offices):
        emit(CHANGE_OFFICE, "offices", _join(sorted(prev.offices)), _join(sorted(curr.offices)))

    if (prev.email, prev.phone) != (curr.email, curr.phone):
        emit(
            CHANGE_CONTACT,
            "contact",
            f"{prev.email or ''} {prev.phone or ''}".strip() or None,
            f"{curr.email or ''} {curr.phone or ''}".strip() or None,
        )

    if prev.profile_url != curr.profile_url:
        emit(CHANGE_PROFILE, "profile_url", prev.profile_url, curr.profile_url)

    return events


def _field(event, name: str):
    """Read ``name`` from a change event that may be a dict or an object."""
    if isinstance(event, dict):
        return event.get(name)
    return getattr(event, name, None)


def detect_lateral_moves(events) -> list[dict]:
    """Heuristically flag people who left one firm and joined another.

    This is **name-based and deliberately conservative about what it claims**:
    given a run's change events it pairs each ``REMOVED`` with each ``ADDED`` that
    shares a normalised name (see :func:`pinpoint.normalize.name_key`) *at a
    different firm*, and reports the pair as a *possible* move.

    It is a heuristic, not record linkage — two different people who happen to
    share a name can produce a false pairing (the bundled fixtures contain exactly
    such a coincidence). That is why its output is surfaced as a clearly-labelled
    "possible moves" view and is never folded into the confirmed change counts.
    Real cross-firm identity needs corroborating signals (education, bar
    admissions, a shared history) which are out of scope here.

    Returns a list of ``{full_name, from_firm, to_firm, detected_at}`` dicts,
    deterministically ordered.
    """
    removed: dict[str, list] = {}
    added: dict[str, list] = {}
    for event in events:
        change_type = _field(event, "change_type")
        key = name_key(_field(event, "full_name"))
        if not key:
            continue
        if change_type == CHANGE_REMOVED:
            removed.setdefault(key, []).append(event)
        elif change_type == CHANGE_ADDED:
            added.setdefault(key, []).append(event)

    moves: list[dict] = []
    for key in sorted(set(removed) & set(added)):
        for add in added[key]:
            for rem in removed[key]:
                from_firm = _field(rem, "firm_slug")
                to_firm = _field(add, "firm_slug")
                if from_firm == to_firm:
                    continue
                moves.append(
                    {
                        "full_name": _field(add, "full_name"),
                        "from_firm": from_firm,
                        "to_firm": to_firm,
                        "detected_at": _field(add, "detected_at") or _field(rem, "detected_at"),
                    }
                )
    return moves
