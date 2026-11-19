"""Unit tests for the ingestion adapters."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs

from pinpoint.adapters import AdapterContext, get_adapter
from pinpoint.adapters.base import AdapterError
from pinpoint.adapters.html_list import HtmlListAdapter
from pinpoint.adapters.json_api import JsonApiAdapter
from pinpoint.config import load_firms
from pinpoint.httppolite import PoliteFetcher
from pinpoint.models import Firm
from pinpoint.pipeline import _normalize_roster

from .helpers import FakeTransport, make_fetcher

CARD_HTML = """
<html><body>
  <nav><a href="/contact">Contact</a></nav>
  <ul>
    <li class="card">
      <a href="/professionals/jane-doe">Jane Doe</a>
      <span class="role">Partner</span>
      <span class="office">New York</span>
      <span class="practices">Corporate, M&A</span>
    </li>
    <li class="card">
      <a href="/professionals/john-smith"><h3>John Smith</h3></a>
      <span class="role">Of Counsel</span>
      <span class="office">London</span>
      <span class="practices">Mergers &amp; Acquisitions</span>
    </li>
    <li class="card">
      <a href="/professionals/jane-doe">Jane Doe</a>  <!-- duplicate -->
      <span class="role">Partner</span>
    </li>
  </ul>
</body></html>
"""


def _html_firm() -> Firm:
    return Firm(
        slug="firm", name="Firm LLP", homepage="https://firm.com",
        partners_url="https://firm.com/professionals",
        profile_pattern="/professionals/[A-Za-z0-9%_-]+",
        options={"offices": ["New York", "London"]},
    )


class HtmlListTests(unittest.TestCase):
    def test_extracts_people_dedupes_and_ignores_nav(self):
        firm = _html_firm()
        people = HtmlListAdapter().parse_listing(
            CARD_HTML, firm, base_url=firm.partners_url,
            pattern=re.compile(firm.profile_pattern),
            known_offices=tuple(firm.options["offices"]),
            assume_corporate=False, default_practices=[],
        )
        # Nav link dropped, duplicate collapsed, both people extracted.
        self.assertEqual(sorted(p.full_name for p in people), ["Jane Doe", "John Smith"])

        jane = next(p for p in people if p.full_name == "Jane Doe")
        self.assertEqual(jane.title, "Partner")
        self.assertEqual(jane.offices, ["New York"])
        self.assertIn("M&A", jane.practices)
        self.assertEqual(jane.profile_url, "https://firm.com/professionals/jane-doe")

    def test_retired_partner_qualifier_is_preserved(self):
        # The qualifier must survive extraction so the pipeline can drop the
        # person from the current roster.
        html = (
            '<ul><li class="card"><a href="/professionals/al-old">Al Old</a>'
            "<span>Retired Partner</span><span>New York</span><span>Corporate</span></li></ul>"
        )
        people = HtmlListAdapter().parse_listing(
            html, _html_firm(), base_url="https://firm.com/professionals",
            pattern=re.compile("/professionals/[A-Za-z0-9_-]+"),
            known_offices=("New York",), assume_corporate=False, default_practices=[],
        )
        self.assertEqual(people[0].title, "Retired Partner")


class FixtureAdapterTests(unittest.TestCase):
    def test_loads_people_and_tags_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "current").mkdir()
            (root / "current" / "firm.json").write_text(
                json.dumps([{"full_name": "Jane Doe", "title": "Partner",
                             "practices": ["Corporate"], "offices": ["New York"]}]),
                encoding="utf-8",
            )
            ctx = AdapterContext(
                fetcher=PoliteFetcher.__new__(PoliteFetcher),
                fixtures_dir=root, fixture_set="current",
            )
            people = get_adapter("fixture").load(
                Firm(slug="firm", name="Firm", homepage="", partners_url=""), ctx
            )
            self.assertEqual(len(people), 1)
            self.assertEqual(people[0].provenance, "sample-fixture")
            self.assertEqual(people[0].firm_slug, "firm")


class DavisPolkTests(unittest.TestCase):
    def _load(self, cache: Path, total: int):
        firm = next(firm for firm in load_firms() if firm.slug == "davispolk")
        public_settings = {
            "algolia_app_id": "TESTAPP123",
            "lawyer_search_index_name": "lawyers",
            "search_key_endpoint": "/api/search/key",
        }
        html = (
            '<script data-drupal-selector="drupal-settings-json">'
            + json.dumps({"dpw_search": public_settings}) + "</script>"
        )
        rows = [
            {"objectID": "1", "name": "Jane Doe", "status": True, "job_title": "Partner",
             "job_title_display": "Partner", "url": "/lawyers/jane-doe",
             "practice": ["Mergers & Acquisitions"], "office": ["New York", "Washington DC"]},
            {"objectID": "2", "name": "John Roe", "status": True, "job_title": "Partner",
             "job_title_display": "Partner", "url": "/lawyers/john-roe", "practice": "Corporate",
             "field_override_job_title": True, "field_job_title_custom": "Special Counsel"},
        ]
        transport = FakeTransport()
        transport.add_substring("/robots.txt", 200, b"User-agent: *\nAllow: /\n")
        transport.add(firm.partners_url, 200, html.encode())

        def search(url: str, headers: dict, *, data: bytes | None = None):
            if data is None:
                return transport(url, headers)
            if url == firm.homepage + "/api/search/key":
                self.assertEqual(headers["Origin"], firm.homepage)
                return 200, {}, b'{"algolia_api_key":"public-test-key"}'
            self.assertEqual(url, "https://testapp123-dsn.algolia.net/1/indexes/*/queries")
            self.assertEqual(headers["X-Algolia-API-Key"], "public-test-key")
            request, = json.loads(data)["requests"]
            params = parse_qs(request["params"])
            self.assertEqual(params["filters"], ['job_title:"Partner"'])
            self.assertEqual(params["page"], ["0"])
            payload = {"results": [{"index": "lawyers", "page": 0, "hits": rows, "nbHits": total,
                                    "nbPages": 1, "exhaustiveNbHits": True}]}
            return 200, {}, json.dumps(payload).encode()

        ctx = AdapterContext(make_fetcher(cache, search), cache, live=True)
        return _normalize_roster(firm, get_adapter(firm.adapter).load(firm, ctx))

    def test_uses_current_title_and_includes_all_offices(self):
        with tempfile.TemporaryDirectory() as tmp:
            person, = self._load(Path(tmp), total=2)
        self.assertEqual(person.full_name, "Jane Doe")
        self.assertEqual(person.practices, ["M&A"])
        self.assertEqual(person.offices, ["New York", "Washington, D.C."])

    def test_rejects_incomplete_directory_results(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(AdapterError, "did not reconcile"):
            self._load(Path(tmp), total=3)


class JsonApiTests(unittest.TestCase):
    def test_maps_json_payload_to_people(self):
        payload = {"data": {"lawyers": [{
            "displayName": "Jane Doe", "title": "Partner",
            "offices": [{"name": "New York"}, {"name": "London"}],
            "practiceAreas": [{"name": "M&A"}],
            "email": "jane@firm.com", "url": "https://firm.com/jane",
        }]}}
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport()
            transport.add_substring("/robots.txt", 200, b"User-agent: *\nAllow: /\n")
            transport.add("https://firm.com/api", 200, json.dumps(payload).encode())
            firm = Firm(
                slug="firm", name="Firm", homepage="", partners_url="https://firm.com/api",
                adapter="json_api",
                options={"list_path": "data.lawyers", "fields": {
                    "full_name": "displayName", "title": "title",
                    "offices": "offices[].name", "practices": "practiceAreas[].name",
                    "email": "email", "profile_url": "url"}},
            )
            people = JsonApiAdapter().load(
                firm, AdapterContext(fetcher=make_fetcher(Path(tmp), transport), fixtures_dir=Path(tmp))
            )

        self.assertEqual(len(people), 1)
        self.assertEqual(people[0].full_name, "Jane Doe")
        self.assertEqual(people[0].offices, ["New York", "London"])
        self.assertEqual(people[0].practices, ["M&A"])
        self.assertEqual(people[0].provenance, "live")


if __name__ == "__main__":
    unittest.main()
