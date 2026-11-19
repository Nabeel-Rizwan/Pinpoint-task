"""Read Simpson Thacher's public paginated staff listing."""

from __future__ import annotations

import json
from urllib.parse import urljoin, urlsplit

from ..dom import parse_html
from ..models import Firm, Person
from ..normalize import is_partner
from .base import Adapter, AdapterContext, AdapterError
from .json_api import _with_query


class SimpsonAdapter(Adapter):
    name = "simpson_thacher"

    def load(self, firm: Firm, ctx: AdapterContext) -> list[Person]:
        people: list[Person] = []
        seen: set[str] = set()
        total = None
        size = 15
        practices = firm.options["practice_filters"]
        for page in range(1, 41):
            url = _with_query(firm.partners_url, {
                "postsPerPage": size, "enablePagination": "true", "viewClass": "wp-grid-view",
                "paged": page, "filterTaxonomies": "client-solution,office,position,school",
                "practice": ",".join(practices), "position": "7972,8135",
            })
            result = ctx.fetcher.fetch(url)
            if not result.ok:
                raise AdapterError(f"{firm.slug}: staff directory fetch failed ({result.reason})")
            payload = json.loads(result.body)
            if not isinstance(payload, dict) or not isinstance(payload.get("html"), str):
                raise AdapterError(f"{firm.slug}: unexpected staff directory response")
            count = payload.get("total_posts")
            if type(count) is not int or count < 1 or (total is not None and total != count):
                raise AdapterError(f"{firm.slug}: staff directory total is missing or changed")
            total = count
            tree = parse_html(payload["html"])
            cards = [a for a in tree.find_all("a") if "wp-card" in (a.get("class") or "").split()]
            if payload.get("start_post") != len(people) + 1 or (
                payload.get("end_post") != len(people) + len(cards)
                or payload.get("current_posts") != len(cards) or not cards or len(cards) > size
            ):
                raise AdapterError(f"{firm.slug}: staff page {page} does not reconcile")
            for card in cards:
                profile = urljoin(firm.homepage, card.get("href") or "")
                parsed = urlsplit(profile)
                if parsed.hostname != urlsplit(firm.homepage).hostname or not parsed.path.startswith("/our-team/partners/"):
                    raise AdapterError(f"{firm.slug}: unexpected staff profile link")
                if profile in seen:
                    raise AdapterError(f"{firm.slug}: repeated staff profile")
                seen.add(profile)
                name = ""
                title = ""
                offices = []
                tags = []
                for node in card.iter():
                    classes = (node.get("class") or "").split()
                    if "wp-avatar-name" in classes:
                        name = node.text_content
                    if "wp-avatar-position" in classes:
                        title = node.text_content
                    if node.get("data-filter-name") == "office":
                        offices.append(node.text_content)
                    if node.get("data-filter-name") == "practice" and node.get("data-filter-id") in practices:
                        tags.append(practices[node.get("data-filter-id")])
                if not name or not is_partner(title) or not tags:
                    raise AdapterError(f"{firm.slug}: a staff card does not confirm partner and practice filters")
                people.append(Person(
                    key="", firm_slug=firm.slug, full_name=name, title=title, offices=offices,
                    practices=tags, profile_url=profile, source_id=parsed.path.rstrip("/"),
                ))
            if len(people) == total:
                ctx.note(f"{firm.slug}: staff directory reconciled {total}/{total} rows")
                return people
            if len(people) > total or len(cards) < size:
                raise AdapterError(f"{firm.slug}: staff directory ended before its reported total")
        raise AdapterError(f"{firm.slug}: staff directory exceeded the page cap")
