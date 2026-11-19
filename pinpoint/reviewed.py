"""Validate a dated offline baseline from approved official-source observations."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .models import Firm, Person
from .normalize import canonical_office, safe_url


@dataclass(frozen=True)
class ReviewedRoster:
    firm: Firm
    source_url: str
    verified_at: str
    evidence: str
    people: list[Person]


def _required_string(row: dict, key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing or invalid {key}")
    return value.strip()


def _string_list(row: dict, key: str, *, allow_empty: bool = False) -> list[str]:
    value = row.get(key)
    if (not isinstance(value, list) or (not value and not allow_empty)
            or any(not isinstance(item, str) or not item.strip() for item in value)):
        raise ValueError(f"{key} must be a list of nonblank strings")
    return [item.strip() for item in value]


def _official_url(value: str, firm: Firm) -> str:
    hosts = {
        urlparse(url).hostname for url in (firm.homepage, firm.partners_url, firm.options.get("practice_url", ""))
    }
    if not safe_url(value) or urlparse(value).scheme != "https" or urlparse(value).hostname not in hosts:
        raise ValueError(f"{firm.slug}: expected an HTTPS URL on a configured official host")
    return value


def load_reviewed(path: Path, firms: list[Firm]) -> list[ReviewedRoster]:
    """Require explicit full-scope evidence and reconciled row and office counts."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("expected a version 1 roster manifest")
    batches = document.get("firms")
    if not isinstance(batches, list) or not batches:
        raise ValueError("the manifest needs at least one firm")
    by_slug = {firm.slug: firm for firm in firms}
    seen: set[str] = set()
    rosters: list[ReviewedRoster] = []
    for batch in batches:
        if not isinstance(batch, dict):
            raise ValueError("expected a firm manifest object")
        slug = _required_string(batch, "firm_slug")
        if slug not in by_slug or slug in seen:
            raise ValueError(f"unknown or duplicate firm in manifest: {slug}")
        seen.add(slug)
        firm = by_slug[slug]
        if batch.get("all_offices_reviewed") is not True:
            raise ValueError(f"{slug}: the manifest must attest that all offices were reviewed")
        evidence = _required_string(batch, "evidence")
        source_url = _official_url(_required_string(batch, "source_url"), firm)
        verified_at = datetime.fromisoformat(_required_string(batch, "verified_at").replace("Z", "+00:00"))
        if verified_at.tzinfo is None or verified_at > datetime.now(timezone.utc):
            raise ValueError(f"{slug}: verified_at must have a timezone and be in the past")
        rows = batch.get("people")
        expected = batch.get("partner_count")
        if not isinstance(rows, list) or type(expected) is not int or expected != len(rows):
            raise ValueError(f"{slug}: partner_count does not reconcile with people")
        people = [_read_person(row, firm) for row in rows]
        offices = Counter(office for person in people for office in set(map(canonical_office, person.offices)))
        if (dict(offices) != batch.get("office_counts")
                or sum(not person.offices for person in people) != batch.get("unknown_office_count", 0)):
            raise ValueError(f"{slug}: office counts disagree with people")
        rosters.append(ReviewedRoster(firm, source_url, verified_at.astimezone(timezone.utc).isoformat(), evidence, people))
    return rosters


def _read_person(row: dict, firm: Firm) -> Person:
    if not isinstance(row, dict):
        raise ValueError(f"{firm.slug}: expected a person object")
    return Person(
        key="", firm_slug=firm.slug, full_name=_required_string(row, "full_name"),
        title=_required_string(row, "title"), practices=_string_list(row, "practices"),
        offices=_string_list(row, "offices", allow_empty=True),
        profile_url=_official_url(_required_string(row, "profile_url"), firm),
        provenance="reviewed", source_id=_required_string(row, "source_id") if row.get("source_id") else None,
    )
