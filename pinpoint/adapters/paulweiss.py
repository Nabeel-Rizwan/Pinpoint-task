"""Read Paul Weiss's public sitemap and the structured sections of each profile."""

from __future__ import annotations

import re

from ..dom import Node, parse_html
from ..models import Firm, Person
from ..normalize import clean_name, normalize_whitespace
from .base import AdapterContext
from .sitemap import SitemapAdapter


class PaulWeissAdapter(SitemapAdapter):
    name = "paul_weiss"

    def load(self, firm: Firm, ctx: AdapterContext) -> list[Person]:
        people = super().load(firm, ctx)
        reviewed_offices = firm.options.get("reviewed_offices", {})
        for person in people:
            if not person.offices and (evidence := reviewed_offices.get(person.source_id)):
                person.offices = list(evidence["offices"])
                ctx.note(f"{firm.slug}: office for profile {person.source_id} verified at {evidence['source_url']}")
        if len({person.source_id for person in people}) != len(people):
            ctx.gaps.append("multiple sitemap URLs resolved to the same profile ID")
        if not ctx.gaps:
            ctx.note(f"{firm.slug}: verified all {len(people)} sitemap-listed partner and counsel profiles")
        return people

    def _profile_to_person(self, firm: Firm, url: str, html: str, offices: tuple[str, ...]) -> Person | None:
        """Use the profile's lawyer metadata and labeled office and practice links."""
        root = parse_html(html)
        metadata = {node.get("name"): node.get("content", "") for node in root.descendants("meta")}
        page_metadata = {node.get("property"): node.get("content", "") for node in root.descendants("meta")}
        heading = root.find("h1")
        source_id = metadata.get("pageid", "")
        title = normalize_whitespace(metadata.get("PositionTitle")).title()
        if (metadata.get("page_category") != "Lawyer" or not source_id.isdecimal() or not title
                or page_metadata.get("og:url") != url or heading is None
                or not (name := clean_name(heading.text_content))):
            return None

        practice_paths = firm.options["corporate_practices"]
        practice_links = _section_links(root, "practices-block", "/practices/")
        if metadata.get("relatedPracticeId") and not practice_links:
            return None
        practices = list(dict.fromkeys(practice_paths[path] for path in practice_links if path in practice_paths))
        office_names = list(_section_links(root, "location-block", "/locations/").values())
        if metadata.get("Location") and not office_names:
            return None
        if title == "Deputy Chair" and re.search(
            r"\bAs a (?:longtime )?partner in the\b", page_metadata.get("og:description", "")[:500], re.I,
        ):
            title += ", Partner"
        return Person(
            key="", firm_slug=firm.slug, full_name=name, title=title,
            practices=practices, offices=office_names, profile_url=url, source_id=source_id, provenance="live",
        )


def _section_links(root: Node, css_class: str, path_prefix: str) -> dict[str, str]:
    return {
        path: anchor.text_content
        for section in root.descendants()
        if css_class in section.get("class", "").split()
        for anchor in section.descendants("a")
        if (path := anchor.get("href", "")).startswith(path_prefix) and anchor.text_content
    }
