"""Fixture adapter.

Loads a firm's roster from a checked-in JSON snapshot. This is what makes the
whole system deterministic and runnable offline: the same code path that can hit
the live web can instead replay a recorded snapshot, which is exactly what we
want for tests, demos and CI.

Fixture layout: ``fixtures/<fixture_set>/<firm-slug>.json`` — a JSON list of
objects with the keys of :meth:`pinpoint.models.Person.to_dict` (``key`` and
``firm_slug`` are optional and filled in here).
"""

from __future__ import annotations

import json

from ..models import Firm, Person
from .base import Adapter, AdapterContext


class FixtureAdapter(Adapter):
    """Read a roster from ``fixtures/<set>/<slug>.json``."""

    name = "fixture"

    def load(self, firm: Firm, ctx: AdapterContext) -> list[Person]:
        """Load and normalise the fixture file for ``firm``."""
        path = ctx.fixtures_dir / ctx.fixture_set / f"{firm.slug}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw["people"] if isinstance(raw, dict) else raw

        people: list[Person] = []
        for index, item in enumerate(items):
            # Tolerate either the full serialised Person shape or a lighter
            # "seed" shape (name/title/practices/offices).
            full_name = item.get("full_name") or item.get("name") or ""
            people.append(
                Person(
                    key=item.get("key") or f"{firm.slug}::fixture-{index}",
                    firm_slug=firm.slug,
                    full_name=full_name,
                    title=item.get("title", ""),
                    practices=list(item.get("practices", item.get("practice", []) or [])),
                    offices=list(item.get("offices", item.get("office", []) or [])),
                    email=item.get("email"),
                    phone=item.get("phone"),
                    profile_url=item.get("profile_url"),
                    provenance="sample-fixture",
                )
            )
        return people

    def enrich(self, person: Person, ctx: AdapterContext) -> Person:
        """No-op: fixtures are already complete."""
        return person
