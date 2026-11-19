"""Use the public search interface configured on Latham's official people page."""

from __future__ import annotations

import json
import re
from dataclasses import replace

from ..models import Firm, Person
from .base import AdapterContext, AdapterError
from .json_api import JsonApiAdapter


class LathamAdapter(JsonApiAdapter):
    name = "latham"

    def load(self, firm: Firm, ctx: AdapterContext) -> list[Person]:
        page = ctx.fetcher.fetch(firm.partners_url, use_cache=False)
        if not page.ok:
            raise AdapterError(f"{firm.slug}: could not open the official people search ({page.reason})")
        org = re.search(r'const organizationId\s*=\s*"([a-zA-Z0-9-]+)"', page.body)
        token = re.search(r'const accessToken\s*=\s*"([a-zA-Z0-9_.-]+)"', page.body)
        if org is None or token is None:
            raise AdapterError(f"{firm.slug}: the public people search configuration changed")
        endpoint = f"https://{org[1]}.org.coveo.com/rest/search/v2"
        directory = replace(firm, partners_url=endpoint)
        headers = {"Authorization": f"Bearer {token[1]}"}
        people = []
        for practice in firm.options["practices"]:
            query = {
                "practice": practice,
                "body": {
                    "q": "",
                    "numberOfResults": 50,
                    "sortCriteria": "@peoplelastname ascending",
                    "aq": (
                        f'@peoplepractices=={json.dumps(practice)} AND @peoplebuckettitle=="Partner" '
                        'AND @z95xlanguage=="en"'
                    ),
                    "fieldsToInclude": [
                        "peopleformattedname", "peopleemployeetitle", "peopleofficesstring", "peoplepractices",
                    ],
                },
                "row_contains": {"raw.peoplepractices": practice},
                "require_partner_titles": True,
            }
            batch = self._paged(directory, ctx, firm.options["pagination"], query, headers)
            for person in batch:
                person.offices = [part.strip() for office in person.offices for part in office.split("/") if part.strip()]
            people.extend(batch)
        return people
