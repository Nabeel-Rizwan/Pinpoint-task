"""Polite HTTP fetching.

The brief's ground rules are explicit: treat the sites we touch as we would in a
real production system. That means, on every request, we:

* identify ourselves with a descriptive ``User-Agent`` plus a contact address,
* honour ``robots.txt`` (and refuse politely when disallowed),
* rate-limit per host so we never burst a single site,
* cache responses to disk and revalidate with conditional requests so repeat
  runs are cheap and don't re-hammer the origin,
* retry transient failures with exponential backoff.

The transport is injectable (``transport=``) so the whole thing is unit-testable
without a network.
"""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from .config import Settings

# A transport takes (url, headers) and returns (status, headers, body-bytes).
Transport = Callable[..., tuple[int, dict[str, str], bytes]]
_REDIRECTS = {301, 302, 303, 307, 308}
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


@dataclass
class FetchResult:
    """The outcome of a single fetch."""

    url: str
    status: int
    body: str
    from_cache: bool
    ok: bool
    reason: str | None = None


class RobotsDisallowed(Exception):
    """Raised when ``robots.txt`` forbids fetching the requested URL."""


def _require_current_robots_parser() -> None:
    """Fail closed on Python runtimes with legacy prefix-only robots matching."""
    parser = RobotFileParser()
    parser.parse([
        "User-agent: *",
        "Allow: /",
        "Disallow: /private/*",
        "Allow: /private/public$",
    ])
    base = "https://example.invalid/private/"
    if parser.can_fetch("PinpointRosterResearch", base + "other") or not parser.can_fetch(
        "PinpointRosterResearch", base + "public",
    ):
        raise ValueError("live crawling needs a current Python 3.14+ release with updated robots.txt rules")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect handler that refuses to follow (returns the 3xx verbatim)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class _DiskCache:
    """Content-addressed on-disk cache for HTTP responses."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def read(self, url: str) -> tuple[str, dict] | None:
        """Return ``(body, meta)`` for a cached URL, or ``None``."""
        try:
            entry = json.loads(self._path(url).read_text(encoding="utf-8"))
            if isinstance(entry["body"], str) and isinstance(entry["meta"], dict):
                return entry["body"], entry["meta"]
        except (OSError, ValueError, TypeError, KeyError):
            pass
        return None

    def write(self, url: str, body: str, meta: dict) -> None:
        """Persist a response body + metadata."""
        fd, filename = tempfile.mkstemp(dir=self.root)
        path = Path(filename)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"body": body, "meta": meta}, handle)
            path.replace(self._path(url))
        finally:
            path.unlink(missing_ok=True)

    def discard(self, url: str) -> None:
        self._path(url).unlink(missing_ok=True)


class PoliteFetcher:
    """Fetch URLs respectfully, with robots, rate-limiting and caching."""

    def __init__(
        self,
        settings: Settings,
        cache_dir: Path | str,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        offline: bool = False,
    ) -> None:
        if transport is None and not offline and settings.respect_robots:
            _require_current_robots_parser()
        self.settings = settings
        self.cache = _DiskCache(Path(cache_dir))
        self._transport = transport or self._urllib_transport
        self._sleep = sleep
        self._clock = clock
        self._offline = offline
        self._last_hit: dict[str, float] = {}       # host -> monotonic timestamp
        self._robots: dict[str, RobotFileParser | None] = {}  # host -> parser
        self._opener: urllib.request.OpenerDirector | None = None

    # ------------------------------------------------------------------ robots
    def _robots_for(self, url: str) -> RobotFileParser | None:
        """Fetch and cache the ``robots.txt`` parser for a URL's host."""
        parsed = urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}"
        if host in self._robots:
            return self._robots[host]

        robots_url = host + "/robots.txt"
        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            # robots.txt itself is read directly, not via the robots gate.
            status, _, body = self._request(
                robots_url, {"User-Agent": self.settings.user_agent}, robots=True
            )
            if status == 200:
                parser.parse(body.decode("utf-8", "replace").splitlines())
            elif status in (404, 410):
                parser.allow_all = True
            else:
                parser.disallow_all = True
        except (OSError, ValueError):
            parser.disallow_all = True
        self._robots[host] = parser
        return parser

    def is_allowed(self, url: str) -> bool:
        """Whether our user-agent may fetch ``url`` per ``robots.txt``."""
        if not self.settings.respect_robots:
            return True
        if self._offline:
            # Offline runs only ever read the cache, so robots is moot.
            return True
        parser = self._robots_for(url)
        if parser is None:
            return True
        return parser.can_fetch(self.settings.user_agent, url)

    # --------------------------------------------------------------- throttling
    def _throttle(self, url: str) -> None:
        """Sleep if needed so we never hit the same host faster than the delay."""
        host = urlparse(url).hostname or ""
        now = self._clock()
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.settings.min_delay_seconds - (now - last)
            if wait > 0:
                self._sleep(wait)
        self._last_hit[host] = self._clock()

    # ----------------------------------------------------------------- fetching
    def _ssl_context(self) -> ssl.SSLContext:
        """Build an SSL context with a usable CA store.

        Order of preference: an explicitly configured bundle, then ``certifi``,
        then common system locations. This matters because a stock Python on
        macOS often ships without a CA store wired up, which would otherwise make
        every HTTPS fetch fail verification.
        """
        if not self.settings.ssl_verify:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            return context

        ca_bundle = self.settings.ca_bundle
        if not ca_bundle:
            try:
                import certifi  # type: ignore

                ca_bundle = certifi.where()
            except Exception:
                for candidate in ("/etc/ssl/cert.pem", "/usr/local/etc/openssl/cert.pem"):
                    if Path(candidate).exists():
                        ca_bundle = candidate
                        break
        try:
            return ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()
        except Exception:
            return ssl.create_default_context()

    def _get_opener(self) -> urllib.request.OpenerDirector:
        """Lazily build (and cache) the urllib opener with our SSL context."""
        if self._opener is None:
            handler = urllib.request.HTTPSHandler(context=self._ssl_context())
            self._opener = urllib.request.build_opener(handler, _NoRedirect())
        return self._opener

    def _request(
        self, url: str, headers: dict[str, str], *, robots: bool = False, search_body: bytes | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        """Throttle each hop and apply the destination page's robots policy."""
        headers = headers.copy()
        for _ in range(6):
            parsed = urlparse(url)
            if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username:
                raise ValueError("expected an HTTP(S) URL without credentials")
            if not robots and not self.is_allowed(url):
                raise RobotsDisallowed(f"robots.txt disallows fetching {url}")
            self._throttle(url)
            if search_body is None:
                status, response_headers, body = self._transport(url, headers)
            else:
                status, response_headers, body = self._transport(url, headers, data=search_body)
            normalized = {key.lower(): value for key, value in response_headers.items()}
            location = normalized.get("location")
            if search_body is not None or not self.settings.follow_redirects or status not in _REDIRECTS or not location:
                return status, normalized, body
            next_url = urljoin(url, location)
            if urlparse(next_url).netloc != parsed.netloc:
                headers.pop("If-None-Match", None)
                headers.pop("If-Modified-Since", None)
            url = next_url
        raise ValueError("too many redirects")

    def _urllib_transport(
        self, url: str, headers: dict[str, str], *, data: bytes | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        """Default transport backed by :mod:`urllib`."""
        request = urllib.request.Request(url, headers=headers, data=data)
        try:
            with self._get_opener().open(request, timeout=self.settings.timeout_seconds) as response:
                body = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(body) > _MAX_RESPONSE_BYTES:
                    raise ValueError("response exceeds the size limit")
                return response.status, dict(response.headers), body
        except urllib.error.HTTPError as exc:  # 4xx/5xx carry a response
            exc.close()
            return exc.code, dict(exc.headers or {}), b""

    def fetch(
        self, url: str, *, use_cache: bool = True, search_body: bytes | None = None,
        search_content_type: str = "application/json", search_headers: dict[str, str] | None = None,
    ) -> FetchResult:
        """Fetch a URL once, honouring robots, rate-limits, cache and retries.

        Fresh cache entries can be reused. Expired entries require successful
        revalidation before they can be used for a refresh.
        """
        if not self.is_allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows fetching {url}")

        use_cache = use_cache and search_body is None
        cached = self.cache.read(url) if use_cache else None

        # Fresh cache wins outright (TTL still valid).
        if cached is not None:
            body, meta = cached
            age = time.time() - float(meta.get("fetched_at", 0))
            if age < self.settings.cache_ttl_seconds:
                return FetchResult(url, 200, body, from_cache=True, ok=True)

        if self._offline:
            if cached is not None:
                return FetchResult(url, 200, cached[0], from_cache=True, ok=True, reason="offline")
            return FetchResult(url, 0, "", from_cache=False, ok=False, reason="offline: no cache")

        headers = {
            "User-Agent": self.settings.user_agent,
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en",
        }
        if search_body is not None:
            headers.update(search_headers or {})
            headers["Content-Type"] = search_content_type
        if cached is not None:
            meta = cached[1]
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]

        last_error: str | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                status, resp_headers, raw = self._request(url, headers, search_body=search_body)
            except OSError as exc:
                last_error = str(exc)
                if not self._retry(attempt):
                    break
                continue

            cache_control = resp_headers.get("cache-control", "").lower()
            no_store = "no-store" in cache_control or not use_cache
            if no_store and cached is not None:
                self.cache.discard(url)
            if status == 304 and cached is not None:
                if not no_store:
                    self.cache.write(url, cached[0], {**cached[1], "fetched_at": time.time()})
                return FetchResult(url, 200, cached[0], from_cache=True, ok=True)

            if 200 <= status < 300:
                body = raw.decode("utf-8", "replace")
                if not no_store:
                    self.cache.write(url, body, {
                        "fetched_at": time.time(),
                        "etag": resp_headers.get("etag"),
                        "last_modified": resp_headers.get("last-modified"),
                    })
                return FetchResult(url, status, body, from_cache=False, ok=True)

            if status in (429, 500, 502, 503, 504):
                last_error = f"HTTP {status}"
                if not self._retry(attempt, resp_headers.get("retry-after")):
                    return FetchResult(url, status, "", from_cache=False, ok=False, reason=last_error)
                continue

            # A definitive 4xx: don't retry, surface to the caller for fallback.
            return FetchResult(url, status, "", from_cache=False, ok=False, reason=f"HTTP {status}")

        return FetchResult(url, 0, "", from_cache=False, ok=False, reason=last_error or "unreachable")

    def _retry(self, attempt: int, retry_after: str | None = None) -> bool:
        """Honor short cooldowns and defer long ones to the next run."""
        if attempt >= self.settings.max_retries:
            return False
        delay = min(self.settings.backoff_base_seconds * (2 ** attempt), 30)
        if retry_after:
            try:
                seconds = int(retry_after)
            except ValueError:
                try:
                    when = parsedate_to_datetime(retry_after)
                    seconds = (when - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    seconds = 0
            if seconds > 60:
                return False
            delay = max(delay, seconds)
        if delay > 0:
            self._sleep(delay)
        return True
