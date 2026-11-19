"""Domain models for the Pinpoint partner-intelligence pipeline.

Everything the pipeline moves around is expressed as an immutable-ish
``dataclass`` here so that the rest of the codebase (adapters, storage,
diffing, reporting) talks about the same shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Firm:
    """A configured law firm and how to ingest its corporate/M&A roster.

    ``adapter`` selects the ingestion strategy (see :mod:`pinpoint.adapters`).
    ``practice_include`` is the list of practice keywords that qualify a person
    as "corporate / M&A"; it is intentionally per-firm because every firm names
    its practice groups slightly differently.
    """

    slug: str
    name: str
    homepage: str
    partners_url: str
    adapter: str = "html_list"
    practice_include: tuple[str, ...] = ()
    profile_pattern: str | None = None
    notes: str = ""
    # Adapter-specific knobs (e.g. JSON field maps, "assume_corporate"). Kept as
    # an open dict so a new adapter can introduce a new option without a schema
    # change to Firm or to the config loader.
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Firm":
        """Build a :class:`Firm` from a JSON config object."""
        return cls(
            slug=data["slug"],
            name=data["name"],
            homepage=data["homepage"],
            partners_url=data["partners_url"],
            adapter=data.get("adapter", "html_list"),
            practice_include=tuple(data.get("practice_include", ())),
            profile_pattern=data.get("profile_pattern"),
            notes=data.get("notes", ""),
            options=dict(data.get("options", {})),
        )


@dataclass
class Person:
    """A single lawyer, normalised into the shape the product cares about.

    ``key`` is the stable identity used for change-detection: it survives across
    refreshes even when profile URLs are rewritten, because it is derived from
    the firm slug plus a punctuation-insensitive form of the name.
    """

    key: str
    firm_slug: str
    full_name: str
    title: str = ""
    practices: list[str] = field(default_factory=list)
    offices: list[str] = field(default_factory=list)
    email: str | None = None
    phone: str | None = None
    profile_url: str | None = None
    provenance: str = "live"  # "live" | "sample-fixture"
    source_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict (used for the JSON API and fixtures)."""
        return {
            "key": self.key,
            "firm_slug": self.firm_slug,
            "full_name": self.full_name,
            "title": self.title,
            "practices": list(self.practices),
            "offices": list(self.offices),
            "email": self.email,
            "phone": self.phone,
            "profile_url": self.profile_url,
            "provenance": self.provenance,
            "source_id": self.source_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Person":
        """Rebuild a :class:`Person` from a serialised dict."""
        return cls(
            key=data["key"],
            firm_slug=data["firm_slug"],
            full_name=data["full_name"],
            title=data.get("title", ""),
            practices=list(data.get("practices", [])),
            offices=list(data.get("offices", [])),
            email=data.get("email"),
            phone=data.get("phone"),
            profile_url=data.get("profile_url"),
            provenance=data.get("provenance", "live"),
            source_id=data.get("source_id"),
        )


# Change types emitted by the diff engine. Kept as constants so callers and
# tests never depend on a bare string literal.
CHANGE_ADDED = "ADDED"
CHANGE_REMOVED = "REMOVED"
CHANGE_TITLE = "TITLE_CHANGED"
CHANGE_PRACTICE = "PRACTICE_CHANGED"
CHANGE_OFFICE = "OFFICE_CHANGED"
CHANGE_CONTACT = "CONTACT_UPDATED"
CHANGE_PROFILE = "PROFILE_UPDATED"

# Firm-level health events. These are deliberately *not* person movement: they
# are raised when a run could not trust a firm's source, so a recruiter sees
# *why* a firm looks unchanged instead of a storm of phantom joins/leaves.
CHANGE_SOURCE_DEGRADED = "SOURCE_DEGRADED"
CHANGE_SOURCE_CHANGED = "SOURCE_CHANGED"
CHANGE_COVERAGE_DROPPED = "COVERAGE_DROPPED"
HEALTH_TYPES = frozenset({CHANGE_SOURCE_DEGRADED, CHANGE_SOURCE_CHANGED, CHANGE_COVERAGE_DROPPED})

# Per-firm ingestion health for a run. ``health`` on FirmSyncResult uses these.
HEALTH_OK = "ok"
HEALTH_SOURCE_DEGRADED = "source-degraded"
HEALTH_COVERAGE_DROPPED = "coverage-dropped"
HEALTH_SOURCE_CHANGED = "source-changed"
#: Statuses that mean "we could not refresh this firm from a trusted source".
DEGRADED_HEALTH = frozenset({HEALTH_SOURCE_DEGRADED, HEALTH_COVERAGE_DROPPED})


@dataclass
class ChangeEvent:
    """A single detected difference between two refreshes."""

    person_key: str
    firm_slug: str
    full_name: str
    change_type: str
    field: str | None = None
    old_value: str | None = None
    new_value: str | None = None
    detected_at: str = ""
    snapshot_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise for storage / the JSON API."""
        return {
            "person_key": self.person_key,
            "firm_slug": self.firm_slug,
            "full_name": self.full_name,
            "change_type": self.change_type,
            "field": self.field,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "detected_at": self.detected_at,
            "snapshot_id": self.snapshot_id,
        }


@dataclass
class FirmSyncResult:
    """Outcome of ingesting one firm during a sync run."""

    firm_slug: str
    firm_name: str
    source: str  # "live" | "sample-fixture"
    fetched: int
    kept: int  # after partner + corporate/M&A filtering
    added: int
    removed: int
    changed: int
    # Ingestion health for this firm in this run (see ``HEALTH_*``). ``health``
    # is one of "ok"/"source-degraded"/"coverage-dropped"/"source-changed";
    # ``health_reason`` is a human-readable explanation.
    health: str = HEALTH_OK
    health_reason: str = ""
    verified_at: str | None = None
    source_url: str | None = None
    error: str | None = None
    # Human-readable diagnostics recorded by the adapter during this firm's run
    # (e.g. "listing parsed -> 42 candidate people"). Surfaced by ``sync --verbose``.
    notes: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        """True when the firm could not be refreshed from a trusted source."""
        return self.health in DEGRADED_HEALTH


@dataclass
class SyncResult:
    """Aggregate outcome of a full sync run."""

    snapshot_id: int
    started_at: str
    finished_at: str
    firms: list[FirmSyncResult] = field(default_factory=list)

    @property
    def total_kept(self) -> int:
        """Number of corporate/M&A partners stored across all firms."""
        return sum(f.kept for f in self.firms)

    @property
    def total_changes(self) -> int:
        """Number of change events raised across all firms."""
        return sum(f.added + f.removed + f.changed for f in self.firms)

    @property
    def degraded_firms(self) -> list[FirmSyncResult]:
        """Firms that could not be refreshed from a trusted source this run."""
        return [f for f in self.firms if f.degraded]
