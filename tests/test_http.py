"""Unit tests for polite fetching.

These guard the brief's "conduct" ground rule: identify yourself, honour
robots.txt, rate-limit per host, and don't re-hammer a site.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pinpoint.config import Settings
from pinpoint.httppolite import PoliteFetcher, RobotsDisallowed

from .helpers import FakeTransport, make_fetcher


class PolitenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_robots_disallow_blocks_fetch(self):
        transport = FakeTransport()
        transport.add_substring("/robots.txt", 200, b"User-agent: *\nDisallow: /people\n")
        fetcher = make_fetcher(self.tmp_path, transport)

        self.assertFalse(fetcher.is_allowed("https://firm.com/people/jane"))
        with self.assertRaises(RobotsDisallowed):
            fetcher.fetch("https://firm.com/people/jane")

    def test_requests_to_same_host_are_spaced(self):
        transport = FakeTransport()
        transport.add_substring("/robots.txt", 200, b"User-agent: *\nAllow: /\n")
        transport.add_substring("/people", 200, b"ok")
        fetcher = make_fetcher(self.tmp_path, transport, min_delay_seconds=1.0)

        fetcher.fetch("https://firm.com/people/a")
        fetcher.fetch("https://firm.com/people/b")

        self.assertTrue(fetcher.slept, "expected a throttle sleep between hits to the same host")
        self.assertGreaterEqual(max(fetcher.slept), 1.0)

    def test_repeat_fetch_is_served_from_cache(self):
        transport = FakeTransport()
        transport.add_substring("/robots.txt", 200, b"User-agent: *\nAllow: /\n")
        transport.add("https://firm.com/people", 200, b"<html>first</html>")
        fetcher = make_fetcher(self.tmp_path, transport)

        first = fetcher.fetch("https://firm.com/people")
        second = fetcher.fetch("https://firm.com/people")

        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        # The page was fetched from the origin exactly once.
        self.assertEqual(transport.hits.get("https://firm.com/people"), 1)

    def test_transient_error_is_retried_then_succeeds(self):
        calls = {"n": 0}

        def transport(url, headers):
            if url.endswith("/robots.txt"):
                return 200, {}, b"User-agent: *\nAllow: /\n"
            calls["n"] += 1
            return (429, {}, b"slow down") if calls["n"] == 1 else (200, {}, b"<html>ok</html>")

        fetcher = make_fetcher(self.tmp_path, transport, max_retries=2)
        self.assertTrue(fetcher.fetch("https://firm.com/people").ok)
        self.assertEqual(calls["n"], 2)

    def test_robots_unreachable_blocks_the_page_request(self):
        transport = FakeTransport()
        transport.add("https://firm.com/robots.txt", 503, b"maintenance")
        fetcher = make_fetcher(self.tmp_path, transport)
        with self.assertRaises(RobotsDisallowed):
            fetcher.fetch("https://firm.com/people")
        self.assertEqual([url for url, _ in transport.calls], ["https://firm.com/robots.txt"])

    def test_legacy_robots_parser_cannot_start_live_crawling(self):
        settings = Settings.from_dict({})
        with patch("pinpoint.httppolite.RobotFileParser") as parser:
            parser.return_value.can_fetch.return_value = True
            with self.assertRaisesRegex(ValueError, "updated robots.txt rules"):
                PoliteFetcher(settings, self.tmp_path)
            offline = PoliteFetcher(settings, self.tmp_path, offline=True)
            self.assertFalse(offline.fetch("https://firm.com/people").ok)

    def test_expired_cache_does_not_turn_outage_into_success(self):
        url = "https://firm.com/people"
        transport = FakeTransport()
        transport.add(url, 200, b"good")
        fetcher = make_fetcher(self.tmp_path, transport, cache_ttl_seconds=0, max_retries=0)
        self.assertTrue(fetcher.fetch(url).ok)
        transport.add(url, 503, b"maintenance")
        self.assertFalse(fetcher.fetch(url).ok)


if __name__ == "__main__":
    unittest.main()
