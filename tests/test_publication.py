"""Regression tests for coverage failures, publication, and identity continuity."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pinpoint.change import firm_status
from pinpoint.models import CHANGE_REMOVED, Firm
from pinpoint.pipeline import publish_reviewed, sync
from pinpoint.reviewed import load_reviewed
from pinpoint.storage import Store

from .helpers import FakeTransport, make_fetcher


FIRM = Firm("one", "One LLP", "https://one.example", "https://one.example/people", "json_api", options={
    "list_path": "people",
    "fields": {
        "source_id": "id", "full_name": "name", "title": "title",
        "offices": "offices", "profile_url": "profile_url", "practices": "practices",
    },
    "pagination": {"offset_param": "offset", "total": "total"},
})


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "roster.db")
        self.addCleanup(self.store.close)
        self.firms = [FIRM]
        self.transport = FakeTransport()
        self.fetcher = make_fetcher(self.root / "cache", self.transport, cache_ttl_seconds=0)

    def row(self, identity, name="Alex Green"):
        return {
            "id": identity, "name": name, "title": "Partner", "offices": ["London"],
            "profile_url": f"https://one.example/{identity}", "practices": ["M&A"],
        }

    def page(self, offset, total, *rows):
        self.transport.add(f"{FIRM.partners_url}?offset={offset}", 200, json.dumps({
            "total": total, "people": rows,
        }).encode())

    def sync(self, **kwargs):
        return sync(self.store, self.firms, self.fetcher, fixtures_dir=self.root, live=True, **kwargs)

    def test_first_failure_does_not_publish_sample_or_empty_verified_roster(self):
        failure = self.sync()
        self.assertTrue(failure.firms[0].degraded)
        self.assertEqual(self.store.total_active(), 0)
        row = self.store.firm_health()[0]
        self.assertEqual(firm_status(row, None), "stale")
        self.assertIsNone(row["last_verified"])

    def test_incomplete_pagination_holds_prior_roster_and_verification_date(self):
        self.page(0, 2, self.row("a"))
        self.page(1, 2, self.row("b"))
        initial = self.sync(now_iso="2025-01-01T00:00:00+00:00")
        self.assertEqual(initial.total_changes, 0)
        self.page(1, 2)
        failed = self.sync(now_iso="2025-01-08T00:00:00+00:00")
        self.assertTrue(failed.firms[0].degraded)
        self.assertEqual(failed.total_kept, 2)
        self.assertEqual(self.store.firm_health()[0]["last_verified"], initial.started_at)

    def test_interrupted_publication_rolls_back_every_write(self):
        self.page(0, 1, self.row("a"))
        with patch.object(self.store, "add_events", side_effect=RuntimeError("publication interrupted")):
            with self.assertRaises(RuntimeError):
                self.sync()
        self.assertIsNone(self.store.latest_snapshot_id())
        self.assertEqual(self.store.total_active(), 0)
        self.assertEqual(self.store.firm_health(), [])

    def test_targeted_refresh_keeps_global_roster_and_prior_firm_health(self):
        other = replace(FIRM, slug="two", name="Two LLP", partners_url="https://two.example/people")
        self.firms.append(other)
        self.page(0, 1, self.row("a"))
        self.transport.add(other.partners_url + "?offset=0", 200, json.dumps({
            "total": 1, "people": [self.row("b")],
        }).encode())
        self.sync()
        self.transport.add(other.partners_url + "?offset=0", 503, b"down")
        self.sync(firm_slugs=["two"])
        result = self.sync(firm_slugs=["one"])
        self.assertEqual(len(self.store.roster_at(result.snapshot_id)), 2)
        self.assertEqual(self.store.list_snapshots(1)[0]["person_count"], 2)
        self.assertEqual(firm_status(self.store.firm_health()[1], None), "stale")

    def test_namesake_keeps_identity_when_other_leaves(self):
        self.page(0, 2, self.row("a"), self.row("b"))
        self.sync()
        key = next(person.key for person in self.store.list_persons() if person.source_id == "b")
        self.page(0, 1, {**self.row("b"), "profile_url": "https://one.example/b-new"})
        result = self.sync()
        self.assertEqual(self.store.list_persons()[0].key, key)
        self.assertEqual([e["change_type"] for e in self.store.changes_for_snapshot(result.snapshot_id)
                          if e["change_type"] == CHANGE_REMOVED], [CHANGE_REMOVED])
        self.assertEqual(result.firms[0].added, 0)

    def test_reviewed_manifest_checks_counts_and_preserves_source_date(self):
        row = self.row("a")
        row["full_name"] = row.pop("name")
        row["source_id"] = row.pop("id")
        firm = {
            "firm_slug": "one", "all_offices_reviewed": True,
            "source_url": FIRM.partners_url, "verified_at": "2025-01-01T00:00:00Z",
            "evidence": "One row reconciled against the official directory.",
            "partner_count": 1, "office_counts": {"London": 1}, "people": [row],
        }
        path = self.root / "reviewed.json"
        path.write_text(json.dumps({"version": 1, "firms": [firm]}))
        result = publish_reviewed(self.store, self.firms, load_reviewed(path, self.firms))
        self.assertEqual(result.total_kept, 1)
        self.assertEqual(result.total_changes, 0)
        self.assertEqual(self.store.firm_health()[0]["last_verified"], "2025-01-01T00:00:00+00:00")
        firm["office_counts"] = {"Paris": 1}
        path.write_text(json.dumps({"version": 1, "firms": [firm]}))
        with self.assertRaisesRegex(ValueError, "office counts"):
            load_reviewed(path, self.firms)
