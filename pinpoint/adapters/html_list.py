"""HTML-list adapter.

Handles the classic server-rendered "people" page: a list of cards, each
containing a link to a profile plus the person's name, title, office and
sometimes practice areas.

Strategy (best-effort by design — firm markup changes constantly):

1. Parse the page with the local DOM.
2. Keep every ``<a>`` whose ``href`` matches the firm's ``profile_pattern``.
3. For each such anchor, use its text as the name and climb to the smallest
   enclosing element that carries extra text ("card") to recover the title,
   office and any practice keywords.
4. Optionally (``--enrich``) fetch each profile page for practices/contact.

Anything we can't parse confidently is simply omitted rather than guessed; the
pipeline then falls back to the recorded fixture for that firm.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from ..dom import Node, parse_html
from ..models import Firm, Person
from ..normalize import (
    canonical_office,
    extract_emails,
    extract_phones,
    practices_in_text,
)
from .base import Adapter, AdapterContext

# Titles we recognise when reading a card's free text, most specific first so
# "Managing Partner" is captured before the bare "Partner". Inactive bands
# (retired/emeritus) are listed first so their qualifier isn't dropped — the
# pipeline then filters them out of the current roster.
_TITLE_RE = re.compile(
    r"\b(Retired Partner|Partner Emeritus|Managing Partner|Senior Partner|"
    r"Equity Partner|Non-Equity Partner|Senior Counsel|Of Counsel|Special Counsel|"
    r"Counsel|Partner|Associate)\b",
    re.IGNORECASE,
)

# Hrefs we never treat as a person (nav/footer noise).
_HREF_BLOCKLIST = re.compile(r"(javascript:|mailto:|#|/contact|/privacy|/terms|/login)", re.IGNORECASE)


class HtmlListAdapter(Adapter):
    """Scrape a server-rendered listing page into people."""

    name = "html_list"

    def load(self, firm: Firm, ctx: AdapterContext) -> list[Person]:
        """Fetch and parse ``firm.partners_url`` into people."""
        result = ctx.fetcher.fetch(firm.partners_url)
        if not result.ok:
            from .base import AdapterError

            raise AdapterError(f"{firm.slug}: listing fetch failed ({result.reason})")

        pattern = _compile_pattern(firm)
        known_offices = tuple(firm.options.get("offices", ()))
        assume_corporate = bool(firm.options.get("assume_corporate", False))
        default_practices = list(firm.options.get("default_practices", ()))

        people = self.parse_listing(
            result.body,
            firm,
            base_url=firm.partners_url,
            pattern=pattern,
            known_offices=known_offices,
            assume_corporate=assume_corporate,
            default_practices=default_practices,
        )
        ctx.note(f"{firm.slug}: listing parsed -> {len(people)} candidate people")
        return people

    def parse_listing(
        self,
        html: str,
        firm: Firm,
        *,
        base_url: str,
        pattern: re.Pattern[str],
        known_offices: tuple[str, ...],
        assume_corporate: bool,
        default_practices: list[str],
    ) -> list[Person]:
        """Pure parsing step (no I/O) so it can be unit-tested directly."""
        root = parse_html(html)
        people: list[Person] = []
        seen_hrefs: set[str] = set()

        for anchor in root.find_all("a"):
            href = (anchor.get("href") or "").strip()
            if not href or _HREF_BLOCKLIST.search(href) or not pattern.search(href):
                continue

            profile_url = urljoin(base_url, href)
            if profile_url in seen_hrefs:
                continue

            name = _anchor_name(anchor)
            if not name or len(name) < 3 or "@" in name:
                continue

            card = _card_text(anchor, name)
            title = _extract_title(card)
            offices = _extract_offices(card, known_offices)
            practices = practices_in_text(card)

            if not practices and assume_corporate:
                practices = list(default_practices) or ["Corporate"]

            seen_hrefs.add(profile_url)
            people.append(
                Person(
                    key="",  # assigned in the pipeline after normalisation
                    firm_slug=firm.slug,
                    full_name=name,
                    title=title or "",
                    practices=practices,
                    offices=offices,
                    profile_url=profile_url,
                    provenance="live",
                )
            )
        return people

    def enrich(self, person: Person, ctx: AdapterContext) -> Person:
        """Fetch the profile page to fill in practices, e-mail and direct dial."""
        if not person.profile_url:
            return person
        if ctx.budget is not None and not ctx.budget.try_take():
            return person
        try:
            result = ctx.fetcher.fetch(person.profile_url)
        except Exception:
            return person
        if not result.ok:
            return person

        page = parse_html(result.body)
        text = page.text_content

        if not person.practices:
            person.practices = practices_in_text(text)
        if not person.email:
            emails = extract_emails(text)
            person.email = emails[0] if emails else None
        if not person.phone:
            phones = extract_phones(text)
            person.phone = phones[0] if phones else None
        # Title from the profile page is more reliable than from the card.
        profile_title = _extract_title(text)
        if profile_title:
            person.title = profile_title
        return person


def _compile_pattern(firm: Firm) -> re.Pattern[str]:
    """Build the profile-link regex, defaulting to the listing path + one slug."""
    if firm.profile_pattern:
        return re.compile(firm.profile_pattern, re.IGNORECASE)
    path = urlparse(firm.partners_url).path.rstrip("/")
    # e.g. "/professionals" -> r"/professionals/[A-Za-z0-9_-]+"
    return re.compile(re.escape(path) + r"/[A-Za-z0-9%_-]+", re.IGNORECASE)


def _anchor_name(anchor: Node) -> str:
    """Best-effort display name from an anchor (text, else a heading child)."""
    for tag in ("h1", "h2", "h3", "h4", "h5", "span", "strong"):
        child = anchor.find(tag)
        if child is not None and child.text_content.strip():
            return _tidy(child.text_content)
    return _tidy(anchor.text_content)


def _tidy(value: str) -> str:
    """Collapse whitespace and strip leading/trailing punctuation."""
    return re.sub(r"\s+", " ", value).strip(" ,|-")


def _card_text(anchor: Node, name: str) -> str:
    """Return the text of the smallest ancestor that adds detail beyond the name.

    Walks up from the anchor and returns the first ancestor whose visible text is
    meaningfully longer than the name — that ancestor is the "card".
    """
    name_len = len(name)
    best: str = anchor.text_content
    for ancestor in anchor.ancestors():
        text = ancestor.text_content
        if len(text) >= name_len + 12 and (
            _TITLE_RE.search(text) or practices_in_text(text) or "," in text
        ):
            return text
        if len(text) > len(best):
            best = text
    return best


def _extract_title(text: str) -> str:
    """Pull the first recognised title phrase out of a blob of text."""
    match = _TITLE_RE.search(text or "")
    return match.group(0).strip() if match else ""


def _extract_offices(text: str, known_offices: tuple[str, ...]) -> list[str]:
    """Return the firm's offices that are mentioned in ``text``."""
    haystack = (text or "").lower()
    found: list[str] = []
    for office in known_offices:
        if office.lower() in haystack and office not in found:
            found.append(canonical_office(office))
    return found
