"""JSON-endpoint adapter.

Several firm sites (and every headless-browser-rendered site, once you find it)
expose the lawyer list as JSON. This adapter maps a JSON payload to people using
a config-supplied field map, so onboarding such a firm is pure configuration:

.. code-block:: json

    {
      "adapter": "json_api",
      "options": {
        "list_path": "data.lawyers",          // dotted path to the array
        "fields": {
          "full_name": "displayName",
          "title": "title",
          "offices": "offices[].name",        // [] marks a list to pluck
          "practices": "practiceAreas[].name",
          "email": "email",
          "phone": "phone",
          "profile_url": "url"
        }
      }
    }
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from ..dom import parse_html
from ..models import Firm, Person
from ..normalize import is_partner, normalize_whitespace
from .base import Adapter, AdapterContext, AdapterError


class JsonApiAdapter(Adapter):
    """Fetch a JSON endpoint and map it to people via ``firm.options``."""

    name = "json_api"

    def load(self, firm: Firm, ctx: AdapterContext) -> list[Person]:
        """Fetch ``firm.partners_url`` as JSON and normalise it."""
        paging = firm.options.get("pagination")
        if paging:
            queries = firm.options.get("queries", [{}])
            return [p for query in queries for p in self._paged(firm, ctx, paging, query)]
        payload = self._fetch(firm, ctx, firm.partners_url)
        rows = _resolve_rows(payload, firm.options.get("list_path", ""))
        return [
            _row_to_person(firm, row, firm.options.get("fields", {}))
            for row in rows if isinstance(row, dict)
        ]

    def _fetch(
        self, firm: Firm, ctx: AdapterContext, url: str, search_body: bytes | None = None,
        search_headers: dict[str, str] | None = None,
    ) -> Any:
        result = ctx.fetcher.fetch(url, search_body=search_body, search_headers=search_headers)
        if not result.ok:
            raise AdapterError(f"{firm.slug}: JSON fetch failed ({result.reason})")
        try:
            return json.loads(result.body)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"{firm.slug}: invalid JSON ({exc})") from exc

    def _paged(
        self, firm: Firm, ctx: AdapterContext, paging: dict, query: dict,
        search_headers: dict[str, str] | None = None,
    ) -> list[Person]:
        """Require the source total, filter echoes, and unique row IDs to reconcile."""
        offset = 0
        expected = None
        ids: set[str] = set()
        people: list[Person] = []
        field_map = firm.options.get("fields", {})
        label = query.get("practice")
        for page in range(paging.get("max_pages", 100)):
            position = page + paging.get("first_page", 0) if paging.get("page_number") else offset
            position_param = {paging["offset_param"]: position}
            params = query.get("params", {})
            method = firm.options.get("search_method", "GET")
            if method == "POST" and "body" in query:
                url = _with_query(firm.partners_url, params)
                search_body = json.dumps({**query["body"], **position_param}).encode()
            else:
                url = _with_query(firm.partners_url, {**params, **position_param})
                search_body = b"" if method == "POST" else None
            payload = self._fetch(firm, ctx, url, search_body, search_headers)
            if (echo := paging.get("position_echo")) and _pluck(payload, echo) != position:
                raise AdapterError(f"{firm.slug}: directory returned the wrong page")
            total = _pluck(payload, paging["total"])
            rows = _pluck(payload, firm.options["list_path"])
            if not isinstance(rows, list) or type(total) is not int or total < 0:
                raise AdapterError(f"{firm.slug}: unexpected directory response")
            if expected is None:
                expected = total
            if total != expected:
                raise AdapterError(f"{firm.slug}: directory total changed during pagination")
            if any(_pluck(payload, path) != value for path, value in query.get("expect", {}).items()):
                raise AdapterError(f"{firm.slug}: the directory did not confirm the requested filters")
            for row in rows:
                if not isinstance(row, dict):
                    raise AdapterError(f"{firm.slug}: malformed person row")
                person = _row_to_person(firm, row, field_map)
                if not person.source_id or person.source_id in ids or not person.full_name or not person.title:
                    raise AdapterError(f"{firm.slug}: missing person fields or repeated directory row")
                ids.add(person.source_id)
                if query.get("title_override"):
                    person.title = query["title_override"]
                if (required_practice := query.get("row_practice")) and required_practice not in person.practices:
                    raise AdapterError(f"{firm.slug}: directory row does not belong to {required_practice}")
                if query.get("require_partner_titles") and not is_partner(person.title):
                    raise AdapterError(f"{firm.slug}: directory row does not confirm the partner filter")
                for field, value in query.get("row_contains", {}).items():
                    observed = _pluck(row, field)
                    if isinstance(value, str) and isinstance(observed, list):
                        observed = [normalize_whitespace(item) for item in observed if isinstance(item, str)]
                    if not isinstance(observed, list) or value not in observed:
                        raise AdapterError(f"{firm.slug}: a directory row does not confirm {label or field}")
                if label:
                    person.practices.append(label)
                if not any(_pluck(row, path) is True for path in firm.options.get("exclude_if_true", [])):
                    people.append(person)
            offset += len(rows)
            more = _pluck(payload, paging["has_more"]) if "has_more" in paging else offset < total
            if type(more) is not bool or more != (offset < total) or offset > total:
                raise AdapterError(f"{firm.slug}: directory pagination disagrees with its reported total")
            if not more:
                ctx.note(f"{firm.slug}: {label or 'directory'} reconciled {offset}/{total} rows")
                return people
            if not rows:
                raise AdapterError(f"{firm.slug}: directory returned an empty page before its reported total")
        raise AdapterError(f"{firm.slug}: page cap reached after {offset}/{expected} directory rows")


def _resolve_rows(payload: Any, list_path: str) -> list[Any]:
    """Return the array of records at ``list_path`` (or the payload itself)."""
    if not list_path:
        return payload if isinstance(payload, list) else [payload]
    node = _pluck(payload, list_path)
    if isinstance(node, list):
        return node
    return []


def _row_to_person(firm: Firm, row: dict[str, Any], field_map: dict[str, str]) -> Person:
    """Map a single JSON record to a :class:`Person`."""
    def pick(field: str) -> Any:
        path = field_map.get(field)
        return _pluck(row, path) if path else None

    full_name = _as_str(pick("full_name")) or ""
    title = _as_str(pick("title")) or ""
    if "<" in title:
        title = parse_html(title).text_content
    offices = _as_list(pick("offices")) + _as_list(pick("secondary_offices"))
    practices = _as_list(pick("practices"))
    profile_url = _as_str(pick("profile_url"))

    return Person(
        key="",  # filled in by the pipeline once names are normalised
        firm_slug=firm.slug,
        full_name=full_name,
        title=title,
        practices=practices,
        offices=offices,
        email=_as_str(pick("email")),
        phone=_as_str(pick("phone")),
        profile_url=urljoin(firm.homepage, profile_url) if profile_url else None,
        provenance="live",
        source_id=_as_str(pick("source_id")),
    )


def _with_query(url: str, params: dict) -> str:
    parts = urlsplit(url)
    encoded = urlencode({**dict(parse_qsl(parts.query)), **params}, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, encoded, ""))


def _pluck(node: Any, path: str) -> Any:
    """Traverse dotted objects with an optional list projection."""
    for index, segment in enumerate(path.split(".")):
        if not isinstance(node, dict):
            return None
        if segment.endswith("[]"):
            seq = node.get(segment[:-2])
            if isinstance(seq, str):
                try:
                    seq = json.loads(seq)
                except json.JSONDecodeError:
                    return None
            if not isinstance(seq, list):
                return None
            tail = ".".join(path.split(".")[index + 1:])
            return [_pluck(item, tail) for item in seq] if tail else seq
        node = node.get(segment)
    return node


def _as_str(value: Any) -> str | None:
    """Coerce a JSON scalar to a stripped string (or ``None``)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    return str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _as_list(value: Any) -> list[str]:
    """Coerce a JSON scalar-or-list to a list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [text for item in value if (text := _as_str(item))]
    return [text] if (text := _as_str(value)) else []
