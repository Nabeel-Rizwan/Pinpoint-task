"""Shared test helpers (a fake HTTP transport and settings)."""

from __future__ import annotations

from pathlib import Path

from pinpoint.config import Settings
from pinpoint.httppolite import PoliteFetcher


class FakeTransport:
    """A scriptable stand-in for real HTTP.

    Routes by exact URL (``routes``) or by substring (``substring_routes``), so a
    test can serve a robots.txt plus one or more pages. Records every call so
    tests can assert on request ordering/headers.
    """

    def __init__(self, routes: dict[str, tuple[int, dict, bytes]] | None = None) -> None:
        self.routes = routes or {}
        self.substring_routes: list[tuple[str, tuple[int, dict, bytes]]] = []
        self.calls: list[tuple[str, dict]] = []
        self.hits: dict[str, int] = {}

    def add(self, url: str, status: int, body: bytes, headers: dict | None = None) -> None:
        """Register an exact-URL response."""
        self.routes[url] = (status, headers or {}, body)

    def add_substring(self, needle: str, status: int, body: bytes, headers: dict | None = None) -> None:
        """Register a response for any URL containing ``needle``."""
        self.substring_routes.append((needle, (status, headers or {}, body)))

    def __call__(self, url: str, headers: dict[str, str]) -> tuple[int, dict, bytes]:
        self.calls.append((url, dict(headers)))
        self.hits[url] = self.hits.get(url, 0) + 1
        if url in self.routes:
            return self.routes[url]
        for needle, response in self.substring_routes:
            if needle in url:
                return response
        return 404, {}, b"not found"


def make_fetcher(tmp_path: Path, transport: FakeTransport, **overrides) -> PoliteFetcher:
    """Build a PoliteFetcher wired to a fake transport and a fake clock."""
    settings = Settings.from_dict(
        {
            "user_agent": "TestBot/1.0",
            "contact": "test@example.com",
            "respect_robots": True,
            "min_delay_seconds": 1.0,
            "timeout_seconds": 5,
            "max_retries": 1,
            "backoff_base_seconds": 0.01,
            "cache_ttl_seconds": 3600,
            **overrides,
        }
    )
    state = {"t": 0.0}
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        state["t"] += seconds

    fetcher = PoliteFetcher(
        settings,
        tmp_path / "cache",
        transport=transport,
        sleep=fake_sleep,
        clock=lambda: state["t"],
    )
    fetcher.slept = slept  # type: ignore[attr-defined]
    return fetcher
