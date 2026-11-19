"""Normalisation helpers.

These functions turn the messy, firm-specific text scraped from the web into a
small canonical vocabulary so that the product can filter ("show me corporate &
M&A partners") and diff ("what changed") reliably. They are pure functions with
no I/O, which makes them cheap to unit-test.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

# Titles/suffixes that are noise when forming an identity key.
_NAME_NOISE = {
    "jr", "sr", "ii", "iii", "iv", "v",
    "esq", "pc", "llp", "phd", "md", "mba", "cfa", "kc", "qc",
}

_WHITESPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[\W_]+", re.UNICODE)

# Geographic tokens that stay upper-case in an office name.
_UPPER_TOKENS = {"us", "uk", "dc", "ny", "la", "sf", "hk", "d.c.", "d.c", "u.s.", "u.k."}

# A title is treated as "partner-level" when it contains this token and is not
# one of the explicitly-excluded seniority bands (associates, counsel, etc.).
_ASSOCIATE_TOKENS = ("associate", "counsel", "summer", "intern", "paralegal")
# People who were partners but are not part of the *current* roster.
_INACTIVE_TOKENS = ("retired", "former", "emeritus", "deceased", "in memoriam", "retiring")
# Sub-strings that do NOT count as partner even though "partner" appears.
_PARTNER_FALSE_POSITIVES = ("partnership", "partnering")


def normalize_whitespace(value: str | None) -> str:
    """Collapse runs of whitespace and trim the result."""
    if not value:
        return ""
    return _WHITESPACE_RE.sub(" ", value).strip()


def strip_diacritics(value: str) -> str:
    """Fold accents so 'José' and 'Jose' hash to the same identity key."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def clean_name(value: str | None) -> str:
    """Tidy a display name: collapse whitespace, fix stray spaces/punctuation."""
    name = normalize_whitespace(value)
    name = re.sub(r"\s*,\s*", ", ", name)  # "Smith ,John" -> "Smith, John"
    name = re.sub(r"\s+([.'-])", r"\1", name)  # "Smith ." -> "Smith."
    return name.strip(" ,")


def name_key(value: str | None) -> str:
    """Return a punctuation/diacritic/accent-insensitive identity token.

    'José A. Ramírez, Jr.' -> 'jose a ramirez' style token (alnum collapsed).
    Used together with the firm slug to form a person's stable ``key``.
    """
    name = strip_diacritics(clean_name(value)).casefold()
    # Reorder "Last, First" -> "first last" so ordering differences don't split identity.
    if "," in name:
        parts = [p.strip() for p in name.split(",") if p.strip()]
        while len(parts) > 1 and parts[-1].rstrip(".") in _NAME_NOISE:
            parts.pop()
        if len(parts) == 2:
            name = f"{parts[1]} {parts[0]}"
        else:
            name = " ".join(parts)
    tokens = [t for t in _NON_ALNUM_RE.split(name) if t and t not in _NAME_NOISE]
    return " ".join(tokens)


def person_key(firm_slug: str, full_name: str) -> str:
    """Build the stable cross-refresh identity key for a person."""
    return f"{firm_slug}::{name_key(full_name)}"


def classify_title(raw_title: str | None) -> str | None:
    """Map a raw title to a canonical seniority band.

    Returns ``"Partner"`` for partner-level roles, the lower-cased band for
    recognised non-partner roles (``"associate"``/``"counsel"``/...), or ``None``
    when the text is empty/unknown. The pipeline keeps only partners.
    """
    title = normalize_whitespace(raw_title).lower()
    if not title:
        return None
    # Retired / former / emeritus people are excluded: this is a *current* view.
    for inactive in _INACTIVE_TOKENS:
        if inactive in title:
            return "inactive"
    if re.match(r"^(?:managing |senior |executive |equity |non-equity )?partner\b", title):
        return "Partner"
    for bad in _ASSOCIATE_TOKENS:
        if bad in title:
            return bad
    if "partner" in title:
        if any(fp in title for fp in _PARTNER_FALSE_POSITIVES):
            return None
        return "Partner"
    return None


def is_partner(raw_title: str | None) -> bool:
    """True when a raw title resolves to a partner-level role."""
    return classify_title(raw_title) == "Partner"


# Canonical practice groups that fall inside "corporate & M&A" for this product.
# Order matters: the first match wins, so more specific groups are listed first.
_PRACTICE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("M&A", ("m&a", "m & a", "mergers & acquisitions", "mergers and acquisitions", "merger", "acquisition", "takeover", "transactional")),
    ("Private Equity", ("private equity", "sponsor", "buyout", "growth equity")),
    ("Capital Markets", ("capital markets", "securities", "ecm", "dcm", "high yield", "ipo")),
    ("Restructuring", ("restructuring", "insolvency", "special situations")),
    ("Banking & Finance", ("banking & finance", "bank finance", "leveraged finance", "finance")),
    ("Corporate", ("corporate", "corporate governance", "public company", "public companies", "general corporate")),
)


def canonical_practice(raw: str | None) -> str | None:
    """Map free-text practice labels to one canonical corporate/M&A group."""
    text = normalize_whitespace(raw).lower()
    if not text:
        return None
    for canonical, needles in _PRACTICE_RULES:
        if any(n in text for n in needles):
            return canonical
    return None


def canonicalize_practices(raw_practices: list[str] | tuple[str, ...]) -> list[str]:
    """Canonicalise + de-duplicate a list of practice labels, keeping order."""
    seen: list[str] = []
    for raw in raw_practices:
        canonical = canonical_practice(raw)
        if canonical and canonical not in seen:
            seen.append(canonical)
    return seen


def matches_corporate_practice(
    raw_practices: list[str] | tuple[str, ...],
    include_keywords: tuple[str, ...] = (),
) -> bool:
    """Decide whether a person belongs to the corporate/M&A superset.

    A person qualifies if *any* of their raw practice labels canonicalise to a
    corporate group (see :data:`_PRACTICE_RULES`) OR contains one of the firm's
    configured ``include_keywords``. The keyword escape-hatch lets a firm whose
    taxonomy we don't recognise still be captured via config rather than code.
    """
    joined = " ".join(normalize_whitespace(p).lower() for p in raw_practices)
    if include_keywords:
        return any(
            re.search(rf"(?<!\w){re.escape(k.casefold())}(?!\w)", joined)
            for k in include_keywords
        )
    return any(canonical_practice(p) is not None for p in raw_practices)


def practices_in_text(text: str | None) -> list[str]:
    """Scan free text for practice keywords and return canonical groups.

    Used by the HTML adapter to recover practice areas from a listing card or a
    profile page when the site does not label them explicitly.
    """
    if not text:
        return []
    haystack = normalize_whitespace(text).lower()
    found: list[str] = []
    for canonical, needles in _PRACTICE_RULES:
        if any(n in haystack for n in needles) and canonical not in found:
            found.append(canonical)
    return found


def canonical_office(raw: str | None) -> str:
    """Normalise an office/city name for consistent display.

    Title-cases each token, keeps a small set of geographic abbreviations in
    caps (US/UK/DC/NY/LA/SF/HK) and renders "D.C." correctly.
    """
    text = normalize_whitespace(raw)
    if not text:
        return ""

    tokens: list[str] = []
    for token in text.split():
        trailing = "," if token.endswith(",") else ""
        bare = token.rstrip(",")
        if bare.lower() in _UPPER_TOKENS:
            rendered = bare.upper()
        else:
            rendered = bare[:1].upper() + bare[1:]
        tokens.append(rendered + trailing)

    result = " ".join(tokens)
    # Tidy the many spellings of the US capital's district.
    result = re.sub(r"\bD\.?\s*C\.?", "D.C.", result, flags=re.IGNORECASE)
    return {"Washington D.C.": "Washington, D.C.", "Wilmington, DE": "Wilmington"}.get(result, result)


def extract_emails(text: str | None) -> list[str]:
    """Pull e-mail addresses out of a blob of text (order-preserving, unique)."""
    if not text:
        return []
    found = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    seen: list[str] = []
    for addr in found:
        if addr not in seen:
            seen.append(addr)
    return seen


def extract_phones(text: str | None) -> list[str]:
    """Pull US/intl-looking phone numbers out of a blob of text."""
    if not text:
        return []
    pattern = re.compile(r"\+?\d[\d().\-\s]{7,}\d")
    found = [normalize_whitespace(m) for m in pattern.findall(text)]
    return [f for f in found if len(re.sub(r"\D", "", f)) >= 9]


# Only these schemes may appear in a rendered link. Anything else (notably
# ``javascript:`` / ``data:``) is dropped so an ingested URL can never become an
# executable href in the dashboard.
_SAFE_URL_SCHEMES = {"http", "https"}


def safe_url(value: str | None) -> str | None:
    """Return ``value`` only when it is a safe absolute ``http(s)`` URL.

    Profile URLs come from third-party sources and are rendered straight into
    ``<a href>``, so the scheme is allow-listed rather than trusted. Returns
    ``None`` for anything else (relative paths, ``javascript:``, garbage).
    """
    text = normalize_whitespace(value)
    if not text:
        return None
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    if parsed.scheme.lower() in _SAFE_URL_SCHEMES and parsed.hostname and not parsed.username:
        return text
    return None
