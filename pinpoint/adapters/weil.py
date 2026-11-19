"""Read Weil's public directory API, including its alphabetical page cursor."""

from __future__ import annotations

import json
from urllib.parse import urljoin, urlsplit

from ..dom import parse_html
from ..models import Firm, Person
from .base import Adapter, AdapterContext, AdapterError
from .json_api import _with_query


class WeilAdapter(Adapter):
    name = "weil"

    def load(self, firm: Firm, ctx: AdapterContext) -> list[Person]:
        return [
            p for label, identifier in firm.options["practice_filters"].items()
            for p in self._practice(firm, ctx, label, identifier)
        ]

    def _practice(self, firm: Firm, ctx: AdapterContext, label: str, identifier: str) -> list[Person]:
        people: list[Person] = []
        seen: set[str] = set()
        cursor = ""
        expected = None
        for number in range(1, 41):
            params = {
                "pagenum": number, "pagesize": 10, "itemId": firm.options["item_id"],
                "practice": identifier,
            }
            if cursor:
                params["lastalphabetnav"] = cursor
            result = ctx.fetcher.fetch(_with_query(firm.partners_url, params))
            if not result.ok:
                raise AdapterError(f"{firm.slug}: directory fetch failed ({result.reason})")
            doc = json.loads(result.body)
            if not isinstance(doc, dict) or doc.get("PageNumber") != number:
                raise AdapterError(f"{firm.slug}: directory returned the wrong page")
            total = doc.get("TotalCount")
            groups = doc.get("SearchGroups")
            if not isinstance(groups, list) or type(total) is not int or total < 1 or (expected is not None and expected != total):
                raise AdapterError(f"{firm.slug}: {label} directory totals are missing or changed")
            expected = total
            rows = [
                html for group in groups if group.get("Title") != "Leadership"
                for html in group["SearchResultItemsAsHtml"]
            ]
            if len(rows) != min(10, total - len(people)):
                raise AdapterError(f"{firm.slug}: {label} directory page does not reconcile")
            for html in rows:
                person = self._person(firm, html, label)
                if person.source_id in seen:
                    raise AdapterError(f"{firm.slug}: repeated directory profile")
                seen.add(person.source_id)
                people.append(person)
            if len(people) == total:
                ctx.note(f"{firm.slug}: {label} reconciled {total}/{total} rows")
                return people
            cursor = doc.get("LastAlphabetNav")
            if not isinstance(cursor, str) or not cursor:
                raise AdapterError(f"{firm.slug}: missing alphabetical continuation")
        raise AdapterError(f"{firm.slug}: page cap reached before the {label} directory ended")

    def _person(self, firm: Firm, html: str, label: str) -> Person:
        tree = parse_html(html)
        heading = tree.find("h3")
        link = heading.find("a") if heading else None
        status = next((n for n in tree.find_all("p") if n.get("class") == "ppl-item-status"), None)
        title = status.find("b") if status else None
        if not link or not link.get("href") or not link.text_content or not title or not title.text_content:
            raise AdapterError(f"{firm.slug}: a directory card is missing a name, title or profile")
        url = urljoin(firm.homepage, link.get("href"))
        parsed = urlsplit(url)
        if parsed.hostname != urlsplit(firm.homepage).hostname or not parsed.path.startswith("/people/"):
            raise AdapterError(f"{firm.slug}: unexpected profile link")
        return Person(
            key="", firm_slug=firm.slug, full_name=link.text_content, title=title.text_content,
            practices=[label], offices=[n.text_content for n in status.find_all("a")], profile_url=url,
            source_id=parsed.path.rstrip("/"),
        )
