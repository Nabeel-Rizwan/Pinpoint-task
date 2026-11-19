"""Integration tests for the read-only JSON API.

Starts the real :class:`ThreadingHTTPServer` from :mod:`pinpoint.server` on an
ephemeral port and hits it with ``urllib`` — exercising routing, paging and the
facet filters end to end (no network, standard library only).
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

from pinpoint import server
from pinpoint.models import ChangeEvent, Firm, Person
from pinpoint.storage import Store


def _person(key: str, name: str, **over) -> Person:
    base = dict(
        key=key, firm_slug="firm", full_name=name, title="Partner",
        practices=["M&A"], offices=["New York"], email="x@firm.com", provenance="live",
    )
    base.update(over)
    return Person(**base)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root / "db.sqlite")
        self.store.upsert_firm(Firm(slug="firm", name="Firm LLP",
                                    homepage="https://firm.com", partners_url="https://firm.com/people"))
        self.store.upsert_persons(
            [
                _person("firm::alice", "Alice Anderson", practices=["M&A"], offices=["London"]),
                _person("firm::bob", "Bob Brown", practices=["Corporate"], offices=["New York"]),
                _person("firm::carol", "Carol Clark", practices=["Corporate"], offices=["New York"]),
            ],
            "2024-01-01T00:00:00+00:00",
        )
        baseline = self.store.start_snapshot("2024-01-01T00:00:00+00:00", "fixture")
        self.store.add_events([ChangeEvent(f"firm::{k}", "firm", k.title(), "ADDED", snapshot_id=baseline)
                               for k in ("alice", "bob", "carol")])
        latest = self.store.start_snapshot("2024-02-01T00:00:00+00:00", "fixture")
        self.store.add_events([
            ChangeEvent("firm::carol", "firm", "Carol Clark", "REMOVED", snapshot_id=latest),
            ChangeEvent("firm::alice", "firm", "Alice Anderson", "TITLE_CHANGED",
                        field="title", old_value="Partner", new_value="Managing Partner", snapshot_id=latest),
        ])
        self.store.finish_snapshot(latest, "2024-02-01T00:01:00+00:00", 1, 3)
        self.latest = latest

        class _Bound(server._Handler):
            pass

        _Bound.store = self.store
        _Bound.firms = []
        _Bound.lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Bound, directory=str(root)))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        self.tmp.cleanup()

    def get(self, path: str):
        with urllib.request.urlopen(self.base + path) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_meta_bootstraps_counts_facets_and_moves(self):
        meta = self.get("/api/meta")
        self.assertEqual(meta["counts"], {"partners": 3, "firms": 1, "changes": 5})
        self.assertEqual(meta["latest_snapshot"], self.latest)
        self.assertEqual(meta["joined"], 0)   # no ADDED in the latest snapshot
        self.assertEqual(meta["left"], 1)     # one REMOVED
        self.assertEqual(meta["provenance"], {"live": 3, "total": 3})
        self.assertEqual([f["slug"] for f in meta["facets"]["firms"]], ["firm"])
        self.assertEqual(meta["facets"]["practices"], ["Corporate", "M&A"])
        self.assertEqual(meta["facets"]["offices"], ["London", "New York"])

    def test_partners_paging_and_facets(self):
        page1 = self.get("/api/partners?limit=2&offset=0")
        self.assertEqual(page1["total"], 3)
        self.assertEqual(len(page1["items"]), 2)
        self.assertEqual([p["full_name"] for p in page1["items"]], ["Alice Anderson", "Bob Brown"])

        page2 = self.get("/api/partners?limit=2&offset=2")
        self.assertEqual([p["full_name"] for p in page2["items"]], ["Carol Clark"])

        london = self.get("/api/partners?office=London")
        self.assertEqual([p["full_name"] for p in london["items"]], ["Alice Anderson"])

        corp_ny = self.get("/api/partners?practice=Corporate&office=New%20York&sort=firm")
        self.assertEqual(corp_ny["total"], 2)

    def test_changes_filter_by_type_and_scope(self):
        latest_only = self.get("/api/changes?snapshot=latest")
        self.assertEqual(latest_only["total"], 2)
        removed = self.get("/api/changes?snapshot=latest&type=REMOVED")
        self.assertEqual([c["full_name"] for c in removed["items"]], ["Carol Clark"])
        all_time = self.get("/api/changes")
        self.assertEqual(all_time["total"], 5)

    def test_person_drilldown_includes_history(self):
        payload = self.get("/api/person?key=firm::alice")
        self.assertEqual(payload["person"]["full_name"], "Alice Anderson")
        self.assertEqual([c["change_type"] for c in payload["history"]], ["TITLE_CHANGED", "ADDED"])

    def test_unknown_endpoint_and_person_are_404(self):
        for path in ("/api/nope", "/api/person?key=firm::ghost"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.get(path)
            self.assertEqual(ctx.exception.code, 404)
            ctx.exception.close()


if __name__ == "__main__":
    unittest.main()
