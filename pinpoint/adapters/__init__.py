"""Adapter registry.

Maps the ``adapter`` string in ``config/firms.json`` to an implementation. Add a
new adapter class, register it here, and any firm can opt into it from config.
"""

from __future__ import annotations

from .base import Adapter, AdapterContext, AdapterError, CrawlBudget
from .davispolk import DavisPolkAdapter
from .fixture import FixtureAdapter
from .gibson import GibsonAdapter
from .html_list import HtmlListAdapter
from .json_api import JsonApiAdapter
from .latham import LathamAdapter
from .milbank import MilbankAdapter
from .paulweiss import PaulWeissAdapter
from .scope import ScopeStatementAdapter
from .simpson import SimpsonAdapter
from .weil import WeilAdapter
from .sitemap import SitemapAdapter

#: Registry of adapter name -> instance. Instances are stateless, so sharing one
#: per process is safe.
REGISTRY: dict[str, Adapter] = {
    FixtureAdapter.name: FixtureAdapter(),
    DavisPolkAdapter.name: DavisPolkAdapter(),
    GibsonAdapter.name: GibsonAdapter(),
    HtmlListAdapter.name: HtmlListAdapter(),
    JsonApiAdapter.name: JsonApiAdapter(),
    LathamAdapter.name: LathamAdapter(),
    MilbankAdapter.name: MilbankAdapter(),
    PaulWeissAdapter.name: PaulWeissAdapter(),
    SitemapAdapter.name: SitemapAdapter(),
    ScopeStatementAdapter.name: ScopeStatementAdapter(),
    SimpsonAdapter.name: SimpsonAdapter(),
    WeilAdapter.name: WeilAdapter(),
}


def get_adapter(name: str) -> Adapter:
    """Return the adapter registered under ``name``.

    Raises :class:`AdapterError` for an unknown name rather than silently
    guessing, so a config typo surfaces as a clear error (``load_firms`` also
    validates the name up front). The pipeline only calls this on the live path,
    where the error degrades to the labelled fixture fallback for that firm.
    """
    try:
        return REGISTRY[name]
    except KeyError as exc:
        raise AdapterError(f"unknown adapter {name!r} (known: {', '.join(sorted(REGISTRY))})") from exc


__all__ = [
    "Adapter",
    "AdapterContext",
    "AdapterError",
    "CrawlBudget",
    "FixtureAdapter",
    "HtmlListAdapter",
    "JsonApiAdapter",
    "SitemapAdapter",
    "REGISTRY",
    "get_adapter",
]
