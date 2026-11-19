"""Unit tests for the storage façade (current state, history, change log)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pinpoint.models import ChangeEvent, Firm, Person
from pinpoint.storage import Store


def make_person(key: str, name: str = "Jane Doe", **overrides) -> Person:
    """Build a Person for storage tests."""
    base = dict(
        key=key,
        firm_slug="firm",
        full_name=name,
        title="Partner",
        practices=["Corporate"],
        offices=["New York"],
        email="jane@firm.com",
        provenance="live",
    )
    base.update(overrides)
    return Person(**base)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")
        self.firm = Firm(
            slug="firm", name="Firm LLP",
            homepage="https://firm.com", partners_url="https://firm.com/people",
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_person_roundtrip_preserves_first_seen(self):
        self.store.upsert_persons([make_person("k")], "2024-01-01T00:00:00+00:00")
        self.store.upsert_persons([make_person("k", title="Managing Partner")], "2024-02-01T00:00:00+00:00")

        person = self.store.get_persons()["k"]
        self.assertEqual(person.title, "Managing Partner")
        self.assertEqual(person.practices, ["Corporate"])
        row = self.store.conn.execute(
            "SELECT first_seen, last_seen FROM persons WHERE person_key='k'"
        ).fetchone()
        self.assertEqual(row["first_seen"], "2024-01-01T00:00:00+00:00")  # preserved
        self.assertEqual(row["last_seen"], "2024-02-01T00:00:00+00:00")

    def test_mark_removed_moves_person_out_of_active(self):
        self.store.upsert_persons([make_person("k")], "2024-01-01T00:00:00+00:00")
        self.store.mark_removed(["k"], "2024-02-01T00:00:00+00:00")
        self.assertEqual(self.store.get_persons(), {})
        self.assertIn("k", self.store.get_persons(status="removed"))

    def test_snapshot_roster_is_recorded(self):
        snapshot_id = self.store.start_snapshot("2024-01-01T00:00:00+00:00", "fixture")
        self.store.save_roster(snapshot_id, [make_person("k")])
        self.store.finish_snapshot(snapshot_id, "2024-01-01T00:01:00+00:00", 1, 1)
        self.assertEqual([p.key for p in self.store.roster_at(snapshot_id)], ["k"])
        self.assertEqual(self.store.latest_snapshot_id(), snapshot_id)

    def test_change_log_records_and_filters(self):
        self.store.add_events(
            [
                ChangeEvent("k", "firm", "Jane", "ADDED", detected_at="2024-01-01T00:00:00+00:00"),
                ChangeEvent("k", "firm", "Jane", "TITLE_CHANGED", field="title",
                            old_value="Partner", new_value="Senior Partner",
                            detected_at="2024-02-01T00:00:00+00:00"),
            ]
        )
        self.assertEqual(self.store.change_count(), 2)
        self.assertEqual(len(self.store.recent_changes(change_type="ADDED")), 1)
        self.assertEqual(self.store.change_type_counts().get("TITLE_CHANGED"), 1)

    def test_list_persons_filters(self):
        self.store.upsert_firm(self.firm)
        self.store.upsert_persons(
            [
                make_person("a", name="Alice Anderson", practices=["M&A"], offices=["London"]),
                make_person("b", name="Bob Brown", practices=["Corporate"], offices=["New York"]),
            ],
            "2024-01-01T00:00:00+00:00",
        )
        self.assertEqual([p.full_name for p in self.store.list_persons(search="Alice")], ["Alice Anderson"])
        self.assertEqual(len(self.store.list_persons(practice="M&A")), 1)
        self.assertEqual(len(self.store.list_persons(office="London")), 1)


if __name__ == "__main__":
    unittest.main()
