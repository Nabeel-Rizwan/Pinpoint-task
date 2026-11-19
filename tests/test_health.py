"""Regression tests for source-aware change detection and per-firm run health.

These pin a real bug: a firm that broke between runs (fell back to the sample
snapshot, or a live parse silently collapsed) used to be diffed against its live
baseline, fabricating a storm of joins/leaves in the feed the product exists to
provide. The fix holds the last-known-good roster and raises one visible event.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pinpoint.change import assess_health
from pinpoint.models import (
    CHANGE_SOURCE_CHANGED,
    CHANGE_SOURCE_DEGRADED,
    CHANGE_COVERAGE_DROPPED,
    HEALTH_COVERAGE_DROPPED,
    HEALTH_OK,
    HEALTH_SOURCE_CHANGED,
    HEALTH_SOURCE_DEGRADED,
    Firm,
    Person,
)
from pinpoint.pipeline import sync
from pinpoint.storage import Store

from .helpers import FakeTransport, make_fetcher

NOW1 = "2024-01-01T00:00:00+00:00"
NOW2 = "2024-02-01T00:00:00+00:00"
API = "https://firm.com/api"


def _firm() -> Firm:
    return Firm(
        slug="firm", name="Firm LLP", homepage="https://firm.com",
        partners_url=API, adapter="json_api",
        options={"list_path": "people", "fields": {
            "full_name": "name", "title": "title",
            "offices": "offices", "practices": "practices"}},
    )


def _payload(names) -> bytes:
    return json.dumps({"people": [
        {"name": n, "title": "Partner", "offices": ["New York"], "practices": ["Corporate"]}
        for n in names]}).encode()


def _transport(api_response) -> FakeTransport:
    transport = FakeTransport()
    transport.add_substring("/robots.txt", 200, b"User-agent: *\nAllow: /\n")
    transport.add(API, *api_response)
    return transport


def _person(key: str, name: str) -> Person:
    return Person(key=key, firm_slug="firm", full_name=name, title="Partner",
                  practices=["Corporate"], offices=["New York"])


class AssessHealthTests(unittest.TestCase):
    def test_first_sighting_is_healthy(self):
        self.assertEqual(assess_health({}, None, 0, "sample-fixture").status, HEALTH_OK)

    def test_live_to_fixture_is_degraded(self):
        prev = {"k": _person("k", "Jane Doe")}
        health = assess_health(prev, "live", 3, "sample-fixture")
        self.assertEqual(health.status, HEALTH_SOURCE_DEGRADED)
        self.assertTrue(health.degraded)

    def test_fixture_to_live_is_a_rebaseline_not_degraded(self):
        prev = {"k": _person("k", "Jane Doe")}
        health = assess_health(prev, "sample-fixture", 3, "live")
        self.assertEqual(health.status, HEALTH_SOURCE_CHANGED)
        self.assertFalse(health.degraded)

    def test_coverage_collapse_is_degraded_but_small_changes_are_not(self):
        prev = {f"k{i}": _person(f"k{i}", f"P{i}") for i in range(8)}
        self.assertEqual(assess_health(prev, "live", 3, "live").status, HEALTH_COVERAGE_DROPPED)
        self.assertEqual(assess_health(prev, "live", 6, "live").status, HEALTH_OK)


class PipelineHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixtures = self.root / "fixtures"
        (self.fixtures / "current").mkdir(parents=True)
        self.store = Store(self.root / "db.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _live(self, run: str, response, now: str):
        transport = _transport(response)
        fetcher = make_fetcher(self.root / run, transport)
        return sync(self.store, [_firm()], fetcher, fixtures_dir=self.fixtures, live=True, now_iso=now)

    def test_source_degradation_holds_roster_and_flags_without_churn(self):
        # A live baseline of six real partners...
        self._live("run1", (200, _payload([f"P{i}" for i in range(6)])), NOW1)
        self.assertEqual(len(self.store.get_persons("firm")), 6)
        # ...then the site starts refusing us, so the run falls back to a
        # two-person sample fixture.
        (self.fixtures / "current" / "firm.json").write_text(
            json.dumps([{"name": "Sample One", "title": "Partner", "practices": ["Corporate"], "offices": ["New York"]},
                        {"name": "Sample Two", "title": "Partner", "practices": ["Corporate"], "offices": ["New York"]}]),
            encoding="utf-8",
        )
        result = self._live("run2", (403, b"forbidden"), NOW2)

        self.assertEqual(result.firms[0].health, HEALTH_SOURCE_DEGRADED)
        self.assertEqual(result.firms[0].added, 0)
        self.assertEqual(result.firms[0].removed, 0)
        self.assertEqual(len(result.degraded_firms), 1)
        # The last-known-good live roster is held, not overwritten by the fixture.
        self.assertEqual(len(self.store.get_persons("firm")), 6)
        # One visible firm-level event, not a storm of phantom movement.
        changes = self.store.changes_for_snapshot(result.snapshot_id)
        self.assertEqual([c["change_type"] for c in changes], [CHANGE_SOURCE_DEGRADED])
        # The held roster is what the snapshot records for this run.
        self.assertEqual(len(self.store.roster_at(result.snapshot_id)), 6)

    def test_coverage_collapse_is_held_not_read_as_attrition(self):
        self._live("run1", (200, _payload([f"P{i}" for i in range(8)])), NOW1)
        result = self._live("run2", (200, _payload([f"P{i}" for i in range(3)])), NOW2)

        self.assertEqual(result.firms[0].health, HEALTH_COVERAGE_DROPPED)
        self.assertEqual(result.firms[0].removed, 0)
        self.assertEqual(len(self.store.get_persons("firm")), 8)
        self.assertEqual(
            [c["change_type"] for c in self.store.changes_for_snapshot(result.snapshot_id)],
            [CHANGE_COVERAGE_DROPPED],
        )

    def test_fixture_to_live_upgrade_rebaselines_without_movement_events(self):
        (self.fixtures / "current" / "firm.json").write_text(
            json.dumps([{"name": "Sample One", "title": "Partner", "practices": ["Corporate"], "offices": ["New York"]}]),
            encoding="utf-8",
        )
        offline = make_fetcher(self.root / "off", FakeTransport())
        sync(self.store, [_firm()], offline, fixtures_dir=self.fixtures, live=False, fixture_set="current")

        result = self._live("run1", (200, _payload(["A", "B", "C", "D", "E"])), NOW1)

        self.assertEqual(result.firms[0].health, HEALTH_SOURCE_CHANGED)
        self.assertEqual(len(self.store.get_persons("firm")), 5)  # adopted the live roster
        self.assertEqual(
            [c["change_type"] for c in self.store.changes_for_snapshot(result.snapshot_id)],
            [CHANGE_SOURCE_CHANGED],
        )


if __name__ == "__main__":
    unittest.main()
