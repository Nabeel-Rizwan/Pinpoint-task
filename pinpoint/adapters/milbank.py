"""Read the public Milbank directory's practice filters and pagination."""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit

from ..dom import Node, parse_html
from ..models import Firm, Person
from .base import Adapter, AdapterContext, AdapterError
from .json_api import _with_query


def _class(node: Node, name: str) -> bool:
    return name in (node.get("class") or "").split()


class MilbankAdapter(Adapter):
    """Extract the roster cards and walk the published next-page controls."""

    name = "milbank"

    def load(self, firm: Firm, ctx: AdapterContext) -> list[Person]:
        people: list[Person] = []
        for practice, practice_id in firm.options["practice_filters"].items():
            people.extend(self._practice(firm, ctx, practice, practice_id))
        return people

    def _practice(self, firm: Firm, ctx: AdapterContext, practice: str, practice_id: str) -> list[Person]:
        offset = 0
        size = 25
        seen: set[str] = set()
        people: list[Person] = []
        for _ in range(40):
            url = _with_query(firm.partners_url, {"p": practice_id, "f": offset, "s": size})
            result = ctx.fetcher.fetch(url)
            if not result.ok:
                raise AdapterError(f"{firm.slug}: {practice} directory fetch failed ({result.reason})")
            tree = parse_html(result.body)
            cards = [node for node in tree.find_all("li") if _class(node, "card--attorney")]
            if not cards or len(cards) > size:
                raise AdapterError(f"{firm.slug}: unexpected card count for {practice} at offset {offset}")
            for card in cards:
                person = self._person(firm, card, practice)
                if person.source_id in seen:
                    raise AdapterError(f"{firm.slug}: a {practice} directory row was repeated")
                if person.source_id is not None:
                    seen.add(person.source_id)
                people.append(person)

            page_active = next(
                (node for node in tree.find_all("li") if _class(node, "search__pagination-button--active")),
                None,
            )
            active_button = page_active.find("button") if page_active is not None else None
            if (offset or page_active is not None) and (
                active_button is None or active_button.get("data-offset") != str(offset)
            ):
                raise AdapterError(f"{firm.slug}: {practice} directory returned the wrong page")

            next_container = next(
                (node for node in tree.find_all("div") if _class(node, "search__pagination-right-arrow")),
                None,
            )
            next_button = next_container.find("button") if next_container is not None else None
            if next_button is None or "disabled" in next_button.attrs:
                if len(cards) == size and page_active is None:
                    raise AdapterError(f"{firm.slug}: cannot establish the end of the {practice} directory")
                ctx.note(f"{firm.slug}: {practice} directory reached its last page with {len(people)} unique rows")
                return people
            if len(cards) != size or next_button.get("data-offset") != str(offset + size):
                raise AdapterError(f"{firm.slug}: unexpected next page for {practice} at offset {offset}")
            offset += size
        raise AdapterError(f"{firm.slug}: hit the page cap before the {practice} directory ended")

    def _person(self, firm: Firm, card: Node, practice: str) -> Person:
        fields: dict[str, list[Node]] = {}
        for node in card.iter():
            for cls in (node.get("class") or "").split():
                fields.setdefault(cls, []).append(node)
        heading = next(iter(fields.get("card__first-line", [])), None)
        title = next(iter(fields.get("card__second-line", [])), None)
        link = heading.find("a") if heading is not None else None
        href = link.get("href") if link is not None else None
        if not href or not link.text_content or title is None or not title.text_content:
            raise AdapterError(f"{firm.slug}: a {practice} card is missing a name, title, or profile")
        url = urljoin(firm.homepage, href)
        parsed = urlsplit(url)
        if parsed.hostname != urlsplit(firm.homepage).hostname or not parsed.path.startswith("/en/professionals/"):
            raise AdapterError(f"{firm.slug}: unexpected profile link")
        return Person(
            key="", firm_slug=firm.slug, full_name=link.text_content, title=title.text_content,
            practices=[practice], offices=[node.text_content for node in fields.get("card__office-name", [])],
            profile_url=url, source_id=parsed.path,
        )
