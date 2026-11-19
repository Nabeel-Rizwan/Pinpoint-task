"""A tiny read-only HTTP server for the dashboard and JSON API.

Deliberately minimal and dependency-free: it serves the static dashboard shell
and a small read-only JSON API over the SQLite store. The dashboard is a thin
client of that API — it fetches only the page it is showing, so data is always
current and nothing is pre-embedded or frozen at report time. Writes, auth and
per-user state are called out as later concerns in the README, not built here.
"""

from __future__ import annotations

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .change import firm_status
from .models import Firm
from .normalize import safe_url
from .storage import Store

#: Upper bound on any single API page, so a client can't ask for everything.
MAX_LIMIT = 200


def _person_payload(person):
    data = person.to_dict()
    data["profile_url"] = safe_url(data["profile_url"])
    return data


def _int(query: dict, name: str, default: int, lo: int = 0, hi: int | None = None) -> int:
    """Parse an int query param with clamping (bad input falls back to default)."""
    try:
        value = int((query.get(name) or [default])[0])
    except (TypeError, ValueError):
        return default
    value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def _str(query: dict, name: str) -> str | None:
    """Parse an optional string query param ('' -> None)."""
    value = (query.get(name) or [None])[0]
    return value or None


class _Handler(SimpleHTTPRequestHandler):
    """Serve the static dashboard shell, plus ``/api/*`` JSON endpoints.

    ``store``/``firms``/``lock`` are bound per server instance in :func:`serve`
    (on a dedicated subclass), so two servers in one process can't clobber each
    other's state. One SQLite connection is shared across request threads, so
    access to it is serialised with the instance lock.
    """

    store: Store
    firms: list[Firm]
    lock: threading.Lock

    def send_head(self):
        """Restrict static files to the generated dashboard and named exports."""
        if urlparse(self.path).path not in ("/", "/index.html", "/partners.csv", "/changes.csv", "/data.json"):
            self.send_error(404)
            return None
        file = "index.html" if urlparse(self.path).path == "/" else urlparse(self.path).path[1:]
        path = Path(self.directory) / file
        if path.is_symlink() or not path.is_file():
            self.send_error(404)
            return None
        return super().send_head()

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                         "base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        super().end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib casing
        """Route ``/api/*`` to JSON, everything else to static files."""
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._handle_api(parsed)
        else:
            super().do_GET()

    def _handle_api(self, parsed) -> None:
        """Dispatch a JSON API request and write the response."""
        query = parse_qs(parsed.query)
        with self.lock, self.store.read_view():
            payload = self._dispatch(parsed.path, query)
        if payload is None:
            return  # a handler already sent an error response
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, path: str, query: dict):
        """Return the JSON payload for a path, or ``None`` after sending an error."""
        if path == "/api/meta":
            return self._meta()
        if path == "/api/firms":
            return self._firms()
        if path == "/api/partners":
            total, items = self.store.page_persons(
                firm_slug=_str(query, "firm"),
                practice=_str(query, "practice"),
                office=_str(query, "office"),
                search=_str(query, "q"),
                sort=_str(query, "sort") or "name",
                limit=_int(query, "limit", 50, 1, MAX_LIMIT),
                offset=_int(query, "offset", 0),
            )
            return {"total": total, "items": [_person_payload(p) for p in items]}
        if path == "/api/changes":
            snapshot = (
                self.store.latest_snapshot_id() if _str(query, "snapshot") == "latest" else None
            )
            total, items = self.store.page_changes(
                firm_slug=_str(query, "firm"),
                change_type=_str(query, "type"),
                snapshot_id=snapshot,
                person_key=_str(query, "person"),
                search=_str(query, "q"),
                limit=_int(query, "limit", 50, 1, MAX_LIMIT),
                offset=_int(query, "offset", 0),
            )
            return {"total": total, "items": items}
        if path == "/api/person":
            person = self.store.get_person(_str(query, "key") or "")
            if person is None:
                self.send_error(404, "Unknown person")
                return None
            _, history = self.store.page_changes(person_key=person.key, limit=100)
            firm = next((f for f in self._firms() if f["slug"] == person.firm_slug), {})
            return {"person": _person_payload(person), "history": history, "firm": firm}
        self.send_error(404, "Unknown API endpoint")
        return None

    def _meta(self) -> dict:
        """Bootstrap payload: counts, per-firm verification, and search facets."""
        store = self.store
        latest = store.latest_snapshot_id()
        flags = store.health_flags_for_snapshot(latest)
        firms = store.firm_health()
        for firm in firms:
            firm["status"] = firm_status(firm, flags.get(firm["slug"]))
        provenance = store.provenance_counts()
        latest_counts = store.change_type_counts_for_snapshot(latest) if latest else {}
        return {
            "latest_snapshot": latest,
            "counts": {
                "partners": provenance.get("total", 0),
                "firms": len(firms),
                "changes": store.change_count(),
            },
            "provenance": {"live": provenance.get("live", 0), "total": provenance.get("total", 0)},
            "sample_count": provenance.get("sample-fixture", 0),
            "joined": latest_counts.get("ADDED", 0),
            "left": latest_counts.get("REMOVED", 0),
            "stale": sum(1 for firm in firms if firm["status"] in ("stale", "coverage gap")),
            "verified": sum(1 for firm in firms if firm["status"] in ("live", "reviewed", "verified empty")),
            "facets": {
                "firms": [{"slug": f["slug"], "name": f["name"]} for f in firms],
                "practices": store.distinct_practices(),
                "offices": store.distinct_offices(),
                "types": store.distinct_change_types(),
            },
        }

    def _firms(self) -> list[dict]:
        """Per-firm coverage + freshness for the firms view."""
        latest = self.store.latest_snapshot_id()
        flags = self.store.health_flags_for_snapshot(latest)
        firms = self.store.firm_health()
        for firm in firms:
            firm["status"] = firm_status(firm, flags.get(firm["slug"]))
        return firms

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        """Quiet the default per-request logging."""
        return


def serve(store: Store, firms: list[Firm], directory: Path | str, host: str = "127.0.0.1", port: int = 8000) -> None:
    """Serve the dashboard (from ``directory``) and the JSON API until Ctrl-C."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the assessment server requires a loopback host")

    class _BoundHandler(_Handler):
        pass

    _BoundHandler.store = store
    _BoundHandler.firms = firms
    _BoundHandler.lock = threading.Lock()

    handler = partial(_BoundHandler, directory=str(directory))
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"Serving dashboard at http://{host}:{port}/  (Ctrl-C to stop)")
    print("JSON API: /api/meta  /api/partners  /api/changes  /api/firms  /api/person")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        httpd.server_close()
