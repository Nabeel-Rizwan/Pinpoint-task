"""Sitemap-driven adapter.

Some firms render their people *list* client-side (so there is nothing to scrape
on the listing page) but still publish a server-rendered ``sitemap.xml`` that
enumerates every profile URL, and each profile page is server-rendered. For those
sites this adapter is the robust way in:

1. read the (people) sitemap,
2. filter its URLs to person profiles,
3. fetch each profile (rate-limited, capped) and extract the fields.

It follows one level of sitemap-index nesting, and always honours a
``max_profiles`` cap so a run stays polite and bounded — full historical coverage
is a scheduled-crawler job, not a synchronous one.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit
from xml.etree import ElementTree

from ..dom import parse_html
from ..httppolite import RobotsDisallowed
from ..models import Firm, Person
from ..normalize import (
    canonical_office,
    clean_name,
    extract_emails,
    extract_phones,
    practices_in_text,
)
from .base import Adapter, AdapterContext, AdapterError
from .html_list import _extract_title

_MAX_CHILD_SITEMAPS = 20


def _read_sitemap(ctx: AdapterContext, url: str) -> tuple[str, list[str]]:
    """Read declared sitemap entries, including properly escaped query strings."""
    result = ctx.fetcher.fetch(url)
    if not result.ok:
        raise AdapterError(f"could not read sitemap {url} ({result.reason})")
    try:
        root = ElementTree.fromstring(result.body)
    except ElementTree.ParseError as exc:
        raise AdapterError(f"invalid XML in sitemap {url}") from exc
    kind = root.tag.rsplit("}", 1)[-1]
    entry = {"sitemapindex": "sitemap", "urlset": "url"}.get(kind)
    if entry is None:
        raise AdapterError(f"unexpected sitemap document at {url}")
    nodes = root.findall(f"{{*}}{entry}/{{*}}loc")
    return kind, [node.text.strip() for node in nodes if node.text and node.text.strip()]


class SitemapAdapter(Adapter):
    """Enumerate profile URLs from a sitemap, then fetch each profile."""

    name = "sitemap"

    def load(self, firm: Firm, ctx: AdapterContext) -> list[Person]:
        """Return people for ``firm`` using its sitemap + profile pages."""
        sitemap_url = firm.options.get("sitemap_url")
        if not sitemap_url:
            raise AdapterError(f"{firm.slug}: sitemap adapter needs options.sitemap_url")

        profile_re = re.compile(firm.options.get("profile_regex") or firm.profile_pattern or r"/people/")
        max_profiles = int(firm.options.get("max_profiles", 25))
        offices = tuple(firm.options.get("offices", ()))

        profile_urls = self._collect_profile_urls(ctx, sitemap_url, profile_re, max_profiles + 1)
        if len(profile_urls) > max_profiles:
            ctx.gaps.append(f"sitemap exceeds the {max_profiles}-profile cap")
        profile_urls = profile_urls[:max_profiles]
        ctx.note(f"{firm.slug}: sitemap -> {len(profile_urls)} profiles (capped at {max_profiles})")

        people: list[Person] = []
        budget = ctx.budget
        for url in profile_urls:
            if budget is not None and not budget.try_take():
                ctx.gaps.append(f"crawl budget exhausted after {len(people)} profiles")
                break
            try:
                result = ctx.fetcher.fetch(url)
            except (OSError, ValueError, RobotsDisallowed):
                ctx.gaps.append(f"could not fetch {url}")
                continue
            if not result.ok:
                ctx.gaps.append(f"could not fetch {url}: {result.reason}")
                if result.status == 429:
                    break
                continue
            person = self._profile_to_person(firm, url, result.body, offices)
            if person is not None:
                people.append(person)
            else:
                ctx.gaps.append(f"could not parse {url}")
        return people

    def _collect_profile_urls(
        self, ctx: AdapterContext, sitemap_url: str, profile_re: re.Pattern[str], cap: int
    ) -> list[str]:
        """Read a sitemap (or index) and return up to ``cap`` profile URLs."""
        kind, urls = _read_sitemap(ctx, sitemap_url)
        target_host = urlsplit(sitemap_url).hostname

        def is_on_source(url: str) -> bool:
            parsed = urlsplit(url)
            return parsed.scheme in ("https", "http") and parsed.hostname == target_host and not parsed.username

        profiles: dict[str, None] = {}

        def add_profiles(candidates: list[str]) -> None:
            for url in candidates:
                if profile_re.search(url):
                    if is_on_source(url):
                        profiles[url] = None
                        if len(profiles) >= cap:
                            return
                    else:
                        ctx.gaps.append("profile sitemap contains an off-host candidate")

        if kind == "urlset":
            add_profiles(urls)
            return list(profiles)

        children = list(dict.fromkeys(urls))
        if len(children) > _MAX_CHILD_SITEMAPS:
            ctx.gaps.append("sitemap index exceeds the child-map cap")
        for child in children[:_MAX_CHILD_SITEMAPS]:
            if not is_on_source(child):
                ctx.gaps.append("sitemap index contains an off-host child map")
                continue
            try:
                child_kind, child_urls = _read_sitemap(ctx, child)
            except (AdapterError, OSError, ValueError, RobotsDisallowed) as exc:
                ctx.gaps.append(str(exc))
                continue
            if child_kind != "urlset":
                ctx.gaps.append("sitemap index contains an unsupported nested index")
                continue
            add_profiles(child_urls)
            if len(profiles) >= cap:
                break
        return list(profiles)

    def _profile_to_person(self, firm: Firm, url: str, html: str, offices: tuple[str, ...]) -> Person | None:
        """Extract a person from a rendered profile page.

        The title is read from the token immediately after the person's name (the
        "Name Title Office" header), and office/contact from a generous window
        after the name. Both are anchored to the name because the full page text
        is full of false positives: the footer lists every office, and unrelated
        people carry their own titles. Practices are inferred from a generous
        window anchored at the name (not the whole page) — a best-effort heuristic.
        """
        root = parse_html(html)
        text = root.text_content

        heading = root.find("h1")
        name = clean_name(heading.text_content) if heading is not None else ""
        if not name or len(name) < 3 or "@" in name:
            return None

        header = _window_after(text, name, size=1200)
        # Practices live in a section below the header, so scan a wider window
        # anchored at the name — but *not* the whole page, or the footer's office
        # list and unrelated profiles inflate the fields.
        scope = _window_after(text, name, size=2000)
        title = _title_near_name(text, name)
        practices = practices_in_text(scope)

        emails = extract_emails(header)
        phones = extract_phones(header)
        found_offices = _offices_in(header, offices) or _offices_in(text, offices)

        return Person(
            key="",
            firm_slug=firm.slug,
            full_name=name,
            title=title,
            practices=practices,
            offices=found_offices,
            email=emails[0] if emails else None,
            phone=phones[0] if phones else None,
            profile_url=url,
            provenance="live",
        )

    def enrich(self, person: Person, ctx: AdapterContext) -> Person:
        """No-op: fields are already read from the profile page itself."""
        return person


def _window_after(text: str, anchor: str, size: int) -> str:
    """Return the slice of ``text`` starting at ``anchor`` (or the whole text).

    The window is generous (default 1200 chars) because a site's name heading is
    usually followed by mega-menu boilerplate before the real profile header
    (name / title / office / contact).
    """
    index = text.find(anchor)
    if index < 0:
        return text
    return text[index : index + size]


def _title_near_name(text: str, name: str) -> str:
    """Find the title token that directly follows a mention of the person's name.

    Profile headers read "Name Title Office ...", so the title is the token
    immediately after the name. Scanning *only* that position avoids picking up
    titles of colleagues or menu entries elsewhere on the page. Returns "" when
    no title follows a mention (the person is then treated as non-partner and
    filtered out — we prefer omission to a wrong label).
    """
    for match in re.finditer(re.escape(name), text):
        title = _extract_title(text[match.end() : match.end() + 40])
        if title:
            return title
    return ""


def _offices_in(text: str, offices: tuple[str, ...]) -> list[str]:
    """Return the offices whose *first* mention appears earliest in ``text``.

    Only offices mentioned within the first 60 characters after the earliest
    match are kept, which captures a person's primary (and co-) offices while
    ignoring the footer's full office list.
    """
    best_pos = None
    for office in offices:
        pos = text.lower().find(office.lower())
        if pos >= 0 and (best_pos is None or pos < best_pos):
            best_pos = pos
    if best_pos is None:
        return []
    window = text[best_pos : best_pos + 60].lower()

    found: list[str] = []
    for office in offices:
        if office.lower() in window and office not in found:
            found.append(canonical_office(office))
    return found
