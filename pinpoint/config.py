"""Configuration loading.

Configuration is split in two:

* ``config/firms.json``      - *what* to ingest (the 15 firms: URLs, adapter
  strategy, offices and other per-firm options).
* ``config/settings.json``   - *how* to ingest it (politeness, caching, UA).

Keeping them separate means adding a 16th firm is a data change, never a code
change, which is the whole point of the adapter registry.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .models import Firm

# Repo root = the directory that contains the ``pinpoint`` package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_FIRMS_PATH = CONFIG_DIR / "firms.json"
DEFAULT_SETTINGS_PATH = CONFIG_DIR / "settings.json"


@dataclass(frozen=True)
class Settings:
    """Politeness / fetching knobs, loaded from ``settings.json``."""

    user_agent: str
    respect_robots: bool
    min_delay_seconds: float
    timeout_seconds: float
    max_retries: int
    backoff_base_seconds: float
    follow_redirects: bool
    cache_ttl_seconds: int
    ca_bundle: str | None
    ssl_verify: bool
    max_profiles_total: int | None

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        """Build settings from a JSON object, applying safe defaults."""
        # A bounded default keeps a run polite out of the box, sized so a ~50-firm fleet
        # (~15 profile-crawling firms x their per-firm cap) is not starved. It is a
        # hard politeness ceiling, first-come-first-served across firms; a much larger
        # crawl belongs in the scheduled worker, not a synchronous run. Set the key to
        # ``null`` explicitly to allow an unbounded crawl.
        raw_cap = data.get("max_profiles_total", 600)
        settings = cls(
            user_agent=data.get(
                "user_agent",
                "PinpointRosterResearch/1.0 (+https://github.com/Nabeel-Rizwan)",
            ),
            respect_robots=bool(data.get("respect_robots", True)),
            min_delay_seconds=float(data.get("min_delay_seconds", 1.5)),
            timeout_seconds=float(data.get("timeout_seconds", 20)),
            max_retries=int(data.get("max_retries", 3)),
            backoff_base_seconds=float(data.get("backoff_base_seconds", 2.0)),
            follow_redirects=bool(data.get("follow_redirects", True)),
            cache_ttl_seconds=int(data.get("cache_ttl_seconds", 86400)),
            ca_bundle=data.get("ca_bundle"),
            ssl_verify=bool(data.get("ssl_verify", True)),
            max_profiles_total=int(raw_cap) if raw_cap is not None else None,
        )
        if not settings.user_agent.strip():
            raise ValueError("user_agent must be nonempty")
        values = (settings.min_delay_seconds, settings.timeout_seconds, settings.backoff_base_seconds)
        if any(not math.isfinite(value) or value < 0 for value in values) or settings.timeout_seconds == 0:
            raise ValueError("fetch delays must be finite and nonnegative, and timeout must be positive")
        if not 0 <= settings.max_retries <= 10 or settings.cache_ttl_seconds < 0:
            raise ValueError("max_retries must be within 0–10 and cache TTL must be nonnegative")
        if settings.max_profiles_total is not None and settings.max_profiles_total < 0:
            raise ValueError("max_profiles_total must be nonnegative or null")
        return settings


def load_settings(path: Path | str | None = None) -> Settings:
    """Load :class:`Settings` from disk, falling back to defaults if absent."""
    p = Path(path) if path else DEFAULT_SETTINGS_PATH
    if not p.exists():
        return Settings.from_dict({})
    return Settings.from_dict(json.loads(p.read_text(encoding="utf-8")))


def load_firms(path: Path | str | None = None) -> list[Firm]:
    """Load and *validate* the configured firms, preserving file order.

    Accepts either a JSON list or ``{"firms": [...]}`` and fails fast (with the
    offending firm slug) on the config mistakes that would otherwise surface as a
    silent zero-live fallback or a mid-run crash: duplicate slugs, an unknown
    ``adapter``, a non-http(s) URL, or a ``profile_pattern``/``profile_regex``
    that does not compile.
    """
    p = Path(path) if path else DEFAULT_FIRMS_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"firm config not found at {p}. Run from the repository root, or install editable "
            f"(`pip install -e .`) so `config/` resolves to the source tree."
        )
    raw = json.loads(p.read_text(encoding="utf-8"))
    items = raw["firms"] if isinstance(raw, dict) else raw
    firms = [Firm.from_dict(item) for item in items]

    # Imported lazily to avoid a config -> adapters -> httppolite import cycle.
    from .adapters import REGISTRY

    seen: set[str] = set()
    for firm in firms:
        if firm.slug in seen:
            raise ValueError(f"Duplicate firm slug in config: {firm.slug!r}")
        seen.add(firm.slug)
        _validate_firm(firm, set(REGISTRY))
    return firms


def _validate_firm(firm: Firm, known_adapters: set[str]) -> None:
    """Raise ``ValueError`` on a firm config that cannot be ingested correctly."""
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", firm.slug):
        raise ValueError(f"invalid firm slug: {firm.slug!r}")
    if not firm.name.strip():
        raise ValueError(f"firm {firm.slug!r}: name is required")
    if firm.adapter not in known_adapters:
        raise ValueError(
            f"firm {firm.slug!r}: unknown adapter {firm.adapter!r} "
            f"(known: {', '.join(sorted(known_adapters))})"
        )
    urls = {"homepage": firm.homepage, "partners_url": firm.partners_url}
    urls.update({k: firm.options[k] for k in ("practice_url", "sitemap_url") if k in firm.options})
    for label, url in urls.items():
        parsed = urlparse(url or "")
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
            raise ValueError(
                f"firm {firm.slug!r}: {label} must be an absolute http(s) URL, got {url!r}"
            )
    for label, pattern in (
        ("profile_pattern", firm.profile_pattern),
        ("options.profile_regex", firm.options.get("profile_regex")),
    ):
        if not pattern:
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"firm {firm.slug!r}: {label} is not a valid regex ({exc})") from exc
    if firm.adapter == "scope_statement" and not firm.options.get("absence_statement"):
        raise ValueError(f"firm {firm.slug!r}: absence_statement is required")
    if firm.adapter == "sitemap" and (
        not firm.options.get("sitemap_url") or type(firm.options.get("max_profiles", 25)) is not int
        or firm.options.get("max_profiles", 25) < 1
    ):
        raise ValueError(f"firm {firm.slug!r}: sitemap_url and a positive profile cap are required")
    if firm.adapter == "json_api" and (paging := firm.options.get("pagination")):
        if not (paging.get("offset_param") and paging.get("total")
                and firm.options.get("list_path") and firm.options.get("fields", {}).get("source_id")):
            raise ValueError(f"firm {firm.slug!r}: paginated JSON requires offset, total, row path and ID fields")
