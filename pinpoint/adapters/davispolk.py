"""Read the partner results from Davis Polk's public lawyer directory."""

from __future__ import annotations

import json
import re
from urllib.parse import urlencode, urljoin

from ..dom import parse_html
from ..models import Firm, Person
from ..normalize import normalize_whitespace
from .base import AdapterContext, AdapterError
from .json_api import JsonApiAdapter, _row_to_person


class DavisPolkAdapter(JsonApiAdapter):
    name = "davis_polk"

    def load(self, firm: Firm, ctx: AdapterContext) -> list[Person]:
        page = ctx.fetcher.fetch(firm.partners_url)
        if not page.ok:
            raise AdapterError(f"{firm.slug}: could not open the public lawyer directory ({page.reason})")

        script = next(
            (node for node in parse_html(page.body).descendants("script")
             if node.get("data-drupal-selector") == "drupal-settings-json"),
            None,
        )
        try:
            config = json.loads(script.children[0].text)["dpw_search"]
            app_id = config["algolia_app_id"]
            index = config["lawyer_search_index_name"]
            key_path = config["search_key_endpoint"]
            if not (re.fullmatch(r"[a-zA-Z0-9]{10}", app_id)
                    and re.fullmatch(r"[a-zA-Z0-9_-]+", index)
                    and re.fullmatch(r"/api/[a-zA-Z0-9_/-]+", key_path)):
                raise ValueError("invalid directory configuration")
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise AdapterError(f"{firm.slug}: the public directory configuration changed") from exc

        public_headers = {"Origin": firm.homepage, "Referer": firm.partners_url}
        key_response = self._fetch(firm, ctx, urljoin(firm.homepage, key_path), b"", public_headers)
        key = key_response.get("algolia_api_key") if isinstance(key_response, dict) else None
        if not isinstance(key, str) or not key.strip():
            raise AdapterError(f"{firm.slug}: the public directory did not provide a search key")
        endpoint = f"https://{app_id.lower()}-dsn.algolia.net/1/indexes/*/queries"
        headers = {**public_headers, "X-Algolia-Application-Id": app_id, "X-Algolia-API-Key": key}
        practice_labels = firm.options["corporate_practices"]

        people: list[Person] = []
        ids: set[str] = set()
        profiles: set[str] = set()
        expected = None
        page_size = 100
        for page_number in range(10):
            params = urlencode({
                "query": "",
                "filters": 'job_title:"Partner"',
                "page": page_number,
                "hitsPerPage": page_size,
                "attributesToRetrieve": json.dumps([
                    "name", "url", "status", "job_title", "job_title_display", "office", "practice",
                    "field_override_job_title", "field_job_title_custom",
                ]),
            })
            body = json.dumps({"requests": [{"indexName": index, "params": params}]}).encode()
            payload = self._fetch(firm, ctx, endpoint, body, headers)
            try:
                result, = payload["results"]
                hits, total, page_count = result["hits"], result["nbHits"], result["nbPages"]
            except (KeyError, TypeError, ValueError) as exc:
                raise AdapterError(f"{firm.slug}: unexpected partner search response") from exc
            if (not isinstance(hits, list) or type(total) is not int or total <= 0
                    or result.get("exhaustiveNbHits") is not True
                    or result.get("index") != index or result.get("page") != page_number
                    or page_count != (total + page_size - 1) // page_size
                    or len(hits) != min(page_size, total - page_number * page_size)
                    or (expected is not None and total != expected)):
                raise AdapterError(f"{firm.slug}: partner search pagination did not reconcile")
            expected = total

            for row in hits:
                if (not isinstance(row, dict) or row.get("job_title") != "Partner"
                        or row.get("status") is not True
                        or not isinstance(row.get("url"), str)
                        or not re.fullmatch(r"/lawyers/[^/?#]+", row["url"])):
                    raise AdapterError(f"{firm.slug}: unexpected row in the active partner results")
                person = _row_to_person(firm, row, {
                    "source_id": "objectID", "full_name": "name", "title": "job_title_display",
                    "offices": "office", "practices": "practice", "profile_url": "url",
                })
                if row.get("field_override_job_title"):
                    person.title = normalize_whitespace(row.get("field_job_title_custom"))
                if (not person.source_id or person.source_id in ids or person.profile_url in profiles
                        or not person.full_name or not person.title):
                    raise AdapterError(f"{firm.slug}: missing or inconsistent partner fields or duplicate result")
                ids.add(person.source_id)
                profiles.add(person.profile_url)
                person.practices = list(dict.fromkeys(
                    practice_labels[practice] for practice in person.practices if practice in practice_labels
                ))
                people.append(person)

            if page_number + 1 == page_count:
                ctx.note(f"{firm.slug}: reconciled all {total} partner-filtered directory rows across all offices")
                return people
        raise AdapterError(f"{firm.slug}: page cap reached after {len(people)}/{expected} partner results")
