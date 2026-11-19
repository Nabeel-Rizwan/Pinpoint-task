"""Unit tests for the change-detection engine — the product's core feature."""

from __future__ import annotations

import unittest

from pinpoint.change import diff_roster
from pinpoint.models import (
    CHANGE_ADDED,
    CHANGE_CONTACT,
    CHANGE_OFFICE,
    CHANGE_PRACTICE,
    CHANGE_REMOVED,
    CHANGE_TITLE,
    Person,
)

NOW = "2024-01-01T00:00:00+00:00"


def person(**overrides) -> Person:
    """Build a Person with sensible defaults for diff testing."""
    base = dict(
        key="firm::jane-doe",
        firm_slug="firm",
        full_name="Jane Doe",
        title="Partner",
        practices=["Corporate"],
        offices=["New York"],
        email="jane@firm.com",
        phone="+1 212 555 0000",
        profile_url="https://firm.com/jane-doe",
    )
    base.update(overrides)
    return Person(**base)


def types(events) -> list[str]:
    """Convenience: the change types of an event list."""
    return [e.change_type for e in events]


class DiffTests(unittest.TestCase):
    def test_added_and_removed(self):
        self.assertEqual(types(diff_roster({}, {"k": person(key="k")}, NOW)), [CHANGE_ADDED])
        self.assertEqual(types(diff_roster({"k": person(key="k")}, {}, NOW)), [CHANGE_REMOVED])

    def test_field_level_changes_are_all_reported(self):
        prev = {"k": person(key="k", title="Partner", offices=["New York"],
                            practices=["Corporate"], email="a@firm.com")}
        curr = {"k": person(key="k", title="Managing Partner", offices=["London"],
                            practices=["Corporate", "M&A"], email="b@firm.com")}
        self.assertEqual(
            set(types(diff_roster(prev, curr, NOW))),
            {CHANGE_TITLE, CHANGE_OFFICE, CHANGE_PRACTICE, CHANGE_CONTACT},
        )

    def test_identical_rosters_produce_no_events(self):
        roster = {"k": person(key="k")}
        self.assertEqual(diff_roster(roster, {"k": person(key="k")}, NOW), [])


if __name__ == "__main__":
    unittest.main()
