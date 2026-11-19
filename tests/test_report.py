"""Tests for the dashboard shell + export generator.

The dashboard is now a thin client of the JSON API, so the shell must reference
the API (not embed data), leave no template tokens, and the exports must be
well-formed — with unsafe profile URLs scrubbed.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pinpoint.models import ChangeEvent, Firm, Person
from pinpoint.report import build_report
from pinpoint.storage import Store


def _person(key: str, name: str, **over) -> Person:
    base = dict(
        key=key, firm_slug="firm", full_name=name, title="Partner",
        practices=["M&A"], offices=["New York"], email="x@firm.com",
        profile_url="https://firm.com/x", provenance="sample-fixture",
    )
    base.update(over)
    return Person(**base)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "db.sqlite")
        self.firm = Firm(slug="firm", name="Firm LLP",
                         homepage="https://firm.com", partners_url="https://firm.com/people")
        self.store.upsert_firm(self.firm)
        self.store.upsert_persons(
            [
                _person("firm::jane", "Jane Doe"),
                _person("firm::eve", "Eve </script>", profile_url="javascript:alert(1)"),
            ],
            "2024-01-01T00:00:00+00:00",
        )
        self.store.add_events(
            [ChangeEvent("firm::jane", "firm", "Jane Doe", "ADDED",
                         detected_at="2024-01-01T00:00:00+00:00")]
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _build(self):
        return build_report(self.store, [self.firm], self.root / "out", "2024-01-02T00:00:00+00:00")

    def test_writes_a_shell_and_exports_with_no_template_tokens(self):
        paths = self._build()
        for key in ("index", "partners_csv", "changes_csv", "data_json"):
            self.assertTrue(paths[key].exists(), key)

        html = (self.root / "out" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("__", html)              # no unreplaced template tokens
        self.assertIn('id="tabs"', html)          # nav shell present
        self.assertIn("/api/meta", html)          # shell is an API client, not embedded data
        self.assertNotIn('id="data"', html)       # nothing pre-embedded / frozen

        csv_text = (self.root / "out" / "partners.csv").read_text(encoding="utf-8")
        self.assertIn("Jane Doe", csv_text)

    def test_data_json_scrubs_unsafe_profile_urls_and_reports_firm_status(self):
        self._build()
        data = json.loads((self.root / "out" / "data.json").read_text(encoding="utf-8"))
        eve = next(p for p in data["partners"] if p["full_name"].startswith("Eve"))
        self.assertIsNone(eve["profile_url"])
        self.assertEqual(data["firms"][0]["slug"], "firm")
        self.assertIn(data["firms"][0]["status"], {"live", "sample", "stale", "no data"})


if __name__ == "__main__":
    unittest.main()
