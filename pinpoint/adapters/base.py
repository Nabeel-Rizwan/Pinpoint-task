"""Adapter base types.

An *adapter* knows how to turn one firm's web presence into a list of
:class:`~pinpoint.models.Person`. Adapters are keyed by the ``adapter`` field in
``config/firms.json`` and live in the :data:`pinpoint.adapters.REGISTRY`.

The design goal: a new firm that ships a standard server-rendered lawyer list or
a JSON endpoint should be onboarded by editing config only — no new Python.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..httppolite import PoliteFetcher
from ..models import Firm, Person


class CrawlBudget:
    """A shared, per-run cap on how many profile pages may be fetched.

    A single budget is created for a sync run and handed to every adapter via
    :class:`AdapterContext`, so the *total* live crawl stays bounded no matter
    how many firms use profile-paging adapters. ``limit=None`` means unbounded
    (each adapter still applies its own ``max_profiles`` cap).
    """

    def __init__(self, limit: int | None = None) -> None:
        self.limit = limit
        self.used = 0

    def try_take(self) -> bool:
        """Reserve one profile fetch; ``False`` once the budget is exhausted."""
        if self.limit is not None and self.used >= self.limit:
            return False
        self.used += 1
        return True


@dataclass
class AdapterContext:
    """Everything an adapter needs to do its job (injected, never global)."""

    fetcher: PoliteFetcher
    fixtures_dir: Path
    fixture_set: str = "current"  # "current" | "next" (etc.)
    live: bool = False            # True when hitting the network
    enrich: bool = False          # fetch per-person profile pages for detail
    budget: CrawlBudget | None = None  # shared cap on profile fetches per run
    log: list[str] = field(default_factory=list)  # human-readable run notes
    gaps: list[str] = field(default_factory=list)
    verified_empty: bool = False

    def note(self, message: str) -> None:
        """Record a diagnostic line for the sync report."""
        self.log.append(message)


class Adapter:
    """Base class for ingestion adapters."""

    #: Registry key; must match ``firm.adapter`` in config.
    name = "base"

    def load(self, firm: Firm, ctx: AdapterContext) -> list[Person]:
        """Return the raw roster for ``firm`` (unfiltered by seniority)."""
        raise NotImplementedError

    def enrich(self, person: Person, ctx: AdapterContext) -> Person:
        """Optionally enrich a person from their profile page.

        Default is a no-op; adapters override when profile pages carry data the
        listing does not (practices, e-mail, direct dial).
        """
        return person


class AdapterError(Exception):
    """Raised by an adapter when live ingestion fails and a fallback is due."""
