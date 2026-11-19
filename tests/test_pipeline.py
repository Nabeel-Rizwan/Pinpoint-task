"""Integration tests for the sync pipeline (pure fixture-driven, no network)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pinpoint.httppolite import PoliteFetcher
from pinpoint.models import Firm
from pinpoint.pipeline import sync
from pinpoint.storage import Store

FIRM = Firm(
    slug="firm",
    name="Firm LLP",
    homepage="https://firm.com",
    partners_url="https://firm.com/professionals",
    practice_include=(),
)

CURRENT = [
    {"full_name": "Jane Doe", "title": "Partner", "practices": ["Corporate"], "offices": ["New York"]},
    {"full_name": "Bob Jones", "title": "Partner", "practices": ["M&A"], "offices": ["London"]},
    {"full_name": "Ann Lee", "title": "Associate", "practices": ["Corporate"], "offices": ["New York"]},
    {"full_name": "Carl Ray", "title": "Partner", "practices": ["Litigation"], "offices": ["New York"]},
]

NEXT = [
    {"full_name": "Jane Doe", "title": "Managing Partner", "practices": ["Corporate"], "offices": ["New York"]},
    {"full_name": "Bob Jones", "title": "Partner", "practices": ["M&A"], "offices": ["Paris"]},
    {"full_name": "Dan X", "title": "Partner", "practices": ["Private Equity"], "offices": ["New York"]},
]


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "fixtures" / "current").mkdir(parents=True)
        (self.root / "fixtures" / "next").mkdir(parents=True)
        (self.root / "fixtures" / "current" / "firm.json").write_text(json.dumps(CURRENT), encoding="utf-8")
        (self.root / "fixtures" / "next" / "firm.json").write_text(json.dumps(NEXT), encoding="utf-8")
        self.store = Store(self.root / "db.sqlite")
        self.fetcher = PoliteFetcher.__new__(PoliteFetcher)  # unused offline

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _sync(self, fixture_set: str, now: str):
        return sync(
            self.store, [FIRM], self.fetcher,
            fixtures_dir=self.root / "fixtures",
            live=False, fixture_set=fixture_set, now_iso=now,
        )

    def test_first_run_filters_and_reports_added(self):
        result = self._sync("current", "2024-01-01T00:00:00+00:00")
        # Associate (Ann) and non-corporate partner (Carl) are filtered out.
        self.assertEqual(result.total_kept, 2)
        self.assertEqual(result.firms[0].added, 2)
        self.assertEqual(result.firms[0].removed, 0)

    def test_second_run_detects_field_changes_and_adds(self):
        self._sync("current", "2024-01-01T00:00:00+00:00")
        result = self._sync("next", "2024-02-01T00:00:00+00:00")

        changes = self.store.recent_changes()
        by_type = {}
        for c in changes:
            by_type.setdefault(c["change_type"], set()).add(c["full_name"])

        self.assertIn("Jane Doe", by_type.get("TITLE_CHANGED", set()))
        self.assertIn("Bob Jones", by_type.get("OFFICE_CHANGED", set()))
        self.assertIn("Dan X", by_type.get("ADDED", set()))
        # Ann (associate) and Carl (litigation) were never stored, so nothing removed.
        self.assertEqual(result.firms[0].removed, 0)

    def test_removed_person_is_detected_and_marked(self):
        self._sync("current", "2024-01-01T00:00:00+00:00")
        # 'next' drops Bob entirely in this scenario.
        next_without_bob = [p for p in NEXT if p["full_name"] != "Bob Jones"]
        (self.root / "fixtures" / "next" / "firm.json").write_text(
            json.dumps(next_without_bob), encoding="utf-8"
        )
        self._sync("next", "2024-02-01T00:00:00+00:00")
        statuses = self.store.get_persons(status=None)
        bob = next(p for p in statuses.values() if p.full_name == "Bob Jones")
        self.assertEqual(self.store.recent_changes(change_type="REMOVED")[0]["full_name"], "Bob Jones")

    def test_snapshot_roster_is_recorded(self):
        result = self._sync("current", "2024-01-01T00:00:00+00:00")
        roster = self.store.roster_at(result.snapshot_id)
        self.assertEqual(len(roster), 2)

    def test_idempotent_rerun_produces_no_changes(self):
        self._sync("current", "2024-01-01T00:00:00+00:00")
        first_changes = len(self.store.recent_changes())
        self._sync("current", "2024-01-02T00:00:00+00:00")
        self.assertEqual(len(self.store.recent_changes()), first_changes)

    def test_same_name_duplicates_get_stable_suffixes(self):
        # Two people who share a name must get an order-independent #N suffix, so a
        # source that merely reorders its people page does not read as a leave+join.
        dupes = [
            {"full_name": "Jane Doe", "title": "Partner", "practices": ["Corporate"],
             "offices": ["New York"], "profile_url": "https://firm.com/jane-doe-ny"},
            {"full_name": "Jane Doe", "title": "Partner", "practices": ["Corporate"],
             "offices": ["London"], "profile_url": "https://firm.com/jane-doe-ldn"},
        ]
        path = self.root / "fixtures" / "current" / "firm.json"
        path.write_text(json.dumps(dupes), encoding="utf-8")
        self._sync("current", "2024-01-01T00:00:00+00:00")
        self.assertEqual(sorted(self.store.get_persons("firm")), ["firm::jane doe#1", "firm::jane doe#2"])

        key_by_office = {tuple(p.offices): k for k, p in self.store.get_persons("firm").items()}
        path.write_text(json.dumps(list(reversed(dupes))), encoding="utf-8")
        result = self._sync("current", "2024-01-02T00:00:00+00:00")

        self.assertEqual({tuple(p.offices): k for k, p in self.store.get_persons("firm").items()}, key_by_office)
        self.assertEqual((result.firms[0].added, result.firms[0].removed), (0, 0))


if __name__ == "__main__":
    unittest.main()
