"""Unit tests for sitemap discovery and profile parsing."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pinpoint.adapters import get_adapter
from pinpoint.adapters.base import AdapterContext
from pinpoint.adapters.sitemap import SitemapAdapter, _offices_in, _title_near_name
from pinpoint.config import load_firms
from pinpoint.models import Firm
from pinpoint.pipeline import _normalize_roster

from .helpers import FakeTransport, make_fetcher

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://firm.com/people</loc></url>
  <url><loc>https://firm.com/people/jane-doe</loc></url>
  <url><loc>https://firm.com/about</loc></url>
  <url><loc>https://firm.com/people/john-roe</loc></url>
</urlset>
"""

PROFILE_JANE = """
<html><body>
  <h1>Jane Doe</h1>
  <nav>skip to content People Capabilities Careers</nav>
  <section><h2>Jane Doe</h2><p>Partner</p><p>New York</p>
    <p>jane.doe@firm.com +1 212 555 1234</p>
    <p>Jane advises on mergers and acquisitions and capital markets.</p></section>
  <footer>Offices: New York London Hong Kong</footer>
</body></html>
"""

PROFILE_JOHN = """
<html><body>
  <h1>John Roe</h1>
  <section><h2>John Roe</h2><p>Associate</p><p>London</p></section>
  <footer>Offices: New York London Hong Kong</footer>
</body></html>
"""


class SitemapAdapterTests(unittest.TestCase):
    def _firm(self, cap: int = 5) -> Firm:
        return Firm(
            slug="firm", name="Firm LLP", homepage="https://firm.com",
            partners_url="https://firm.com/people", adapter="sitemap",
            options={
                "sitemap_url": "https://firm.com/sitemap.xml",
                "profile_regex": r"^https://firm\.com/people/[a-z0-9-]+$",
                "max_profiles": cap,
                "offices": ["New York", "London", "Hong Kong"],
            },
        )

    def _ctx(self, tmp: Path) -> AdapterContext:
        transport = FakeTransport()
        transport.add_substring("/robots.txt", 200, b"User-agent: *\nAllow: /\n")
        transport.add("https://firm.com/sitemap.xml", 200, SITEMAP.encode())
        transport.add("https://firm.com/people/jane-doe", 200, PROFILE_JANE.encode())
        transport.add("https://firm.com/people/john-roe", 200, PROFILE_JOHN.encode())
        return AdapterContext(fetcher=make_fetcher(tmp, transport), fixtures_dir=tmp, live=True)

    def test_loads_only_profile_urls_and_extracts_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = SitemapAdapter().load(self._firm(), self._ctx(Path(tmp)))

        # '/people' and '/about' are not profiles, so only the two people load.
        self.assertEqual([p.full_name for p in people], ["Jane Doe", "John Roe"])
        jane = people[0]
        self.assertEqual(jane.title, "Partner")
        self.assertEqual(jane.offices, ["New York"])  # footer offices ignored
        self.assertEqual(jane.email, "jane.doe@firm.com")
        self.assertIn("M&A", jane.practices)
        self.assertEqual(jane.provenance, "live")

    def test_respects_max_profiles_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            people = SitemapAdapter().load(self._firm(cap=1), self._ctx(Path(tmp)))
        self.assertEqual([p.full_name for p in people], ["Jane Doe"])

    def test_follows_index_pages_with_queries_and_deduplicates_profiles(self):
        index = """<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <sitemap><loc>https://firm.com/sitemap.xml?page=1&amp;lang=en</loc></sitemap>
          <sitemap><loc>https://firm.com/sitemap.xml?page=2&amp;lang=en</loc></sitemap>
        </sitemapindex>"""
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp))
            ctx.fetcher._transport.add("https://firm.com/sitemap.xml", 200, index.encode())
            ctx.fetcher._transport.add(
                "https://firm.com/sitemap.xml?page=1&lang=en", 200,
                b"<urlset><url><loc>https://firm.com/people/jane-doe</loc></url></urlset>",
            )
            ctx.fetcher._transport.add("https://firm.com/sitemap.xml?page=2&lang=en", 200, SITEMAP.encode())
            people = SitemapAdapter().load(self._firm(cap=2), ctx)
        self.assertEqual([p.full_name for p in people], ["Jane Doe", "John Roe"])
        self.assertFalse(ctx.gaps)

    def test_header_extraction_helpers(self):
        text = "Jane Doe skip to content Jane Doe Partner New York"
        self.assertEqual(_title_near_name(text, "Jane Doe"), "Partner")
        self.assertEqual(_offices_in("Partner New York jane@firm.com",
                                     ("New York", "London")), ["New York"])


class PaulWeissTests(unittest.TestCase):
    def test_sitemap_reads_labeled_sections_with_directory_search_blocked(self):
        firm = next(firm for firm in load_firms() if firm.slug == "paulweiss")
        profile = firm.homepage + "/professionals/partners-and-counsel/jane-doe"
        html = f"""
        <meta property="og:url" content="{profile}">
        <meta name="page_category" content="Lawyer">
        <meta name="pageid" content="7">
        <meta name="PositionTitle" content="partner">
        <meta name="Location" content="London; New York">
        <meta name="relatedPracticeId" content="23, 24">
        <h1>Jane<br>Doe</h1>
        <div class="location-block">
          <a href="/locations/london">London</a>
          <a href="/locations/new-york">New York</a>
        </div>
        <div class="practices-block">
          <a href="/practices/transactional/mergers-acquisitions">Mergers &amp; Acquisitions</a>
          <a href="/practices/litigation/mergers-acquisitions-litigation">M&amp;A Litigation</a>
        </div>
        <footer>
          <a href="/locations/toronto">Toronto</a>
          <a href="/practices/transactional/finance">Finance</a>
          <a href="mailto:jane@example.com">Contact</a>
        </footer>"""
        transport = FakeTransport()
        transport.add_substring("/robots.txt", 200, b"User-agent: *\nDisallow: /umbraco/\n")
        sitemap = f"<urlset><url><loc>{profile}</loc></url></urlset>"
        transport.add(firm.options["sitemap_url"], 200, sitemap.encode())
        transport.add(profile, 200, html.encode())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            ctx = AdapterContext(make_fetcher(path, transport), path, live=True)
            adapter = get_adapter(firm.adapter)
            person, = _normalize_roster(firm, adapter.load(firm, ctx))
        self.assertFalse(ctx.gaps)
        self.assertEqual(person.full_name, "Jane Doe")
        self.assertEqual(person.offices, ["London", "New York"])
        self.assertEqual(person.practices, ["M&A"])
        self.assertEqual(person.source_id, "7")
        self.assertIsNone(person.email)
        self.assertEqual(set(transport.hits), {firm.homepage + "/robots.txt", firm.options["sitemap_url"], profile})

    def test_executive_title_requires_explicit_partner_status(self):
        firm = next(firm for firm in load_firms() if firm.slug == "paulweiss")
        profile = firm.homepage + "/professionals/partners-and-counsel/jane-doe"
        html = f"""
        <meta property="og:url" content="{profile}">
        <meta name="page_category" content="Lawyer">
        <meta name="pageid" content="7">
        <meta name="PositionTitle" content="Deputy Chair">
        <h1>Jane Doe</h1>
        <div class="practices-block"><a href="/practices/transactional/finance">Finance</a></div>
        """
        adapter = get_adapter(firm.adapter)
        lead = '<meta property="og:description" content="As a longtime partner in the Finance group, Jane advises clients.">'
        self.assertEqual(adapter._profile_to_person(firm, profile, html + lead, ()).title, "Deputy Chair, Partner")
        self.assertEqual(adapter._profile_to_person(firm, profile, html, ()).title, "Deputy Chair")


if __name__ == "__main__":
    unittest.main()
