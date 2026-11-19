"""Verify an explicit statement that a firm has no in-scope practice."""

from __future__ import annotations

from ..dom import parse_html
from ..models import Firm, Person
from ..normalize import normalize_whitespace
from .base import Adapter, AdapterContext, AdapterError


class ScopeStatementAdapter(Adapter):
    """Distinguish a verified empty roster from a failing directory."""

    name = "scope_statement"

    def load(self, firm: Firm, ctx: AdapterContext) -> list[Person]:
        result = ctx.fetcher.fetch(firm.partners_url)
        if not result.ok:
            raise AdapterError(f"{firm.slug}: could not verify practice scope ({result.reason})")
        statement = normalize_whitespace(firm.options["absence_statement"]).casefold()
        text = normalize_whitespace(parse_html(result.body).text_content).casefold()
        if not statement or statement not in text:
            raise AdapterError(f"{firm.slug}: practice scope statement changed; review it before publishing")
        ctx.verified_empty = True
        ctx.note(f"{firm.slug}: the official practice page confirms a zero-partner transactional scope")
        return []
