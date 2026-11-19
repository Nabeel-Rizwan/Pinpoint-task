"""Consume Gibson Dunn's public directory facet results."""

from __future__ import annotations

import json
import re
from urllib.parse import urlencode, urljoin, urlsplit

from ..dom import Node, parse_html
from ..models import Firm, Person
from ..normalize import is_partner
from .base import Adapter, AdapterContext, AdapterError
from .json_api import _with_query


def _has_class(node: Node, name: str) -> bool:
    return name in (node.get("class") or "").split()


class GibsonAdapter(Adapter):
    name = "gibson_dunn"

    def load(self, firm: Firm, ctx: AdapterContext) -> list[Person]:
        return [
            person
            for label, practice in firm.options["practice_filters"].items()
            for person in self._practice(firm, ctx, label, practice)
        ]

    def _practice(self, firm: Firm, ctx: AdapterContext, label: str, practice: str) -> list[Person]:
        config = {
            "is_main_query": False, "main_query": [], "permalink": "https://www.gibsondunn.com/people/",
            "facets": [42, 18, 34], "lang": "", "id": "archivePeople", "is_template": True,
        }
        body = urlencode({"wpgb": json.dumps(config)}).encode()
        expected = None
        people: list[Person] = []
        seen: set[str] = set()
        for _ in range(50):
            params = {"_people_practices": practice, "_people_title": "000_partner"}
            if people:
                params["_people_load_more"] = str(len(people))
            url = _with_query(firm.partners_url, params)
            result = ctx.fetcher.fetch(url, search_body=body, search_content_type="application/x-www-form-urlencoded")
            if not result.ok:
                raise AdapterError(f"{firm.slug}: directory fetch failed ({result.reason})")
            payload = json.loads(result.body)
            if not isinstance(payload, dict) or not isinstance(payload.get("posts"), str):
                raise AdapterError(f"{firm.slug}: unexpected directory response")
            facets = payload.get("facets", {})
            if facets.get("42", {}).get("selected") != [practice] or facets.get("18", {}).get("selected") != ["000_partner"]:
                raise AdapterError(f"{firm.slug}: directory did not confirm the practice and partner filters")
            total = payload.get("total")
            if (type(total) is not int or total < firm.options.get("practice_minimums", {}).get(label, 1)
                    or (expected is not None and expected != total)):
                raise AdapterError(f"{firm.slug}: {label} total is missing, below the verified floor, or changed")
            expected = total
            tree = parse_html(payload["posts"])
            cards = [n for n in tree.find_all("div") if _has_class(n, "people")]
            if not cards or len(people) + len(cards) > total:
                raise AdapterError(f"{firm.slug}: directory page does not reconcile")
            for card in cards:
                person = self._person(firm, card, label)
                if person.profile_url in seen:
                    raise AdapterError(f"{firm.slug}: directory repeated a profile")
                seen.add(person.profile_url)
                people.append(person)
            if len(people) == total:
                ctx.note(f"{firm.slug}: {label} reconciled {total}/{total} rows")
                return people
        raise AdapterError(f"{firm.slug}: {label} directory exceeded the page cap")

    def _person(self, firm: Firm, card: Node, practice: str) -> Person:
        link = card.find("a")
        title_node = next((n for n in card.find_all("p") if _has_class(n, "title")), None)
        title = title_node.find("span") if title_node else None
        if not link or not link.get("href") or not link.text_content or not title or not is_partner(title.text_content):
            raise AdapterError(f"{firm.slug}: malformed or out-of-scope directory card")
        profile = urljoin(firm.homepage, link.get("href"))
        parsed = urlsplit(profile)
        if parsed.hostname != urlsplit(firm.homepage).hostname or not parsed.path.startswith("/lawyer/"):
            raise AdapterError(f"{firm.slug}: unexpected profile link")
        contact = next((n for n in card.find_all("p") if _has_class(n, "contact-details")), None)
        offices = []
        if contact:
            for child in contact.children:
                if child.tag == "#text" and child.text.strip() and child.text.strip() != "T:":
                    offices.extend(
                        value.strip() for value in re.split(r"\||\s+and\s+", child.text, flags=re.I)
                        if value.strip()
                    )
        return Person(
            key="", firm_slug=firm.slug, full_name=link.text_content, title=title.text_content,
            offices=offices, practices=[practice], profile_url=profile, source_id=parsed.path.rstrip("/"),
        )
