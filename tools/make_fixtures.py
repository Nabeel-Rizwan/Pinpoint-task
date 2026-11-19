"""Generate the checked-in fixtures used for offline/deterministic runs.

Why generated, not scraped? The brief's sites are JS-heavy and bot-protected, so
the *only* way to guarantee a reproducible, runnable submission is to ship a
recorded snapshot alongside the live adapters. These fixtures stand in for "what
the site looked like on day X" and are clearly marked ``sample-fixture`` in the
product so nobody mistakes them for verified, live data.

Two sets are produced:

* ``fixtures/current/<slug>.json`` - the baseline roster.
* ``fixtures/next/<slug>.json``    - the same roster a week later, with a handful
  of deliberate changes (a joiner, a leaver, a promotion, an office move, a
  practice change, a new direct dial) so change detection has something to show.

Run: ``python3 tools/make_fixtures.py``
"""

from __future__ import annotations

import json
import random
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"

# The generator reuses the product's own canonicaliser so that a "practice
# change" we inject actually differs *after* normalisation (e.g. appending
# "Securities" when "Capital Markets" is already present would be a no-op).
sys.path.insert(0, str(ROOT))
from pinpoint.normalize import canonical_practice, canonicalize_practices  # noqa: E402

FIRST_NAMES = [
    "James", "Sarah", "Michael", "Emily", "David", "Laura", "Robert", "Katherine",
    "Andrew", "Rachel", "Daniel", "Hannah", "Christopher", "Olivia", "Jonathan",
    "Sophia", "Matthew", "Charlotte", "Nicholas", "Amelia", "Alexander", "Grace",
    "Benjamin", "Zoe", "Samuel", "Nadia", "Adam", "Priya", "Ethan", "Yuki",
    "Marcus", "Elena", "Patrick", "Ingrid", "Thomas", "Mei", "Peter", "Ana",
]
LAST_NAMES = [
    "Whitfield", "Hargrove", "Sinclair", "Vance", "Blackwood", "Ashford", "Kensington",
    "Langford", "Merriweather", "Thornton", "Pemberton", "Ashworth", "Radcliffe",
    "Winstanley", "Barrow", "Hollingsworth", "Fairchild", "Quimby", "Ellsworth",
    "Wexford", "Sanders", "Nakamura", "Okafor", "Fernandez", "Kowalski", "Rossi",
    "Petrov", "Dubois", "Andersen", "Silva", "Cohen", "Rahman", "Sharma", "Chen",
    "Ibrahim", "Novak", "Mancini", "Bekker",
]

CORPORATE_PRACTICES = [
    "Corporate", "M&A", "Private Equity", "Capital Markets", "Banking & Finance",
    "Restructuring", "Corporate Governance", "Securities",
]
NON_CORPORATE_PRACTICES = [
    "Litigation", "Intellectual Property", "Tax", "Antitrust", "Real Estate",
    "Executive Compensation & Benefits", "White Collar Defense", "Environmental",
]
PARTNER_TITLES = ["Partner", "Partner", "Partner", "Senior Partner", "Managing Partner"]
NON_PARTNER_TITLES = ["Associate", "Counsel", "Of Counsel", "Special Counsel"]


def _rng(slug: str) -> random.Random:
    """A deterministic RNG seeded from a firm slug."""
    return random.Random(zlib.crc32(slug.encode("utf-8")))


def _name(rng: random.Random) -> str:
    """Compose a plausible full name from the pools."""
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"


def _email(name: str, slug: str) -> str:
    """Derive a plausible firm e-mail from a name and firm slug."""
    return name.lower().replace(" ", ".") + f"@{slug}.example.invalid"


def _phone(rng: random.Random) -> str:
    """Generate a plausible direct dial."""
    area = rng.choice(["212", "415", "310", "202", "312"])
    rng.randint(200, 999)  # Preserve the sequence used to create the other demo fields.
    return f"+1 {area} 555 {100 + rng.randint(1000, 9999) % 100:04d}"


def _person(rng: random.Random, slug: str, profile_base: str, offices: list[str]) -> dict:
    """Build one partner record."""
    name = _name(rng)
    practices = rng.sample(CORPORATE_PRACTICES, k=rng.randint(1, 2))
    office = [rng.choice(offices)]
    return {
        "full_name": name,
        "title": rng.choice(PARTNER_TITLES),
        "practices": practices,
        "offices": office,
        "email": _email(name, slug),
        "phone": _phone(rng),
        "profile_url": f"{profile_base}/{name.lower().replace(' ', '-')}",
    }


def _decoy(rng: random.Random, slug: str, profile_base: str, offices: list[str]) -> dict:
    """A non-corporate or non-partner record that the pipeline must filter out."""
    name = _name(rng)
    non_partner = rng.random() < 0.5
    return {
        "full_name": name,
        "title": rng.choice(NON_PARTNER_TITLES) if non_partner else "Partner",
        "practices": [rng.choice(NON_CORPORATE_PRACTICES)],
        "offices": [rng.choice(offices)],
        "email": _email(name, slug),
        "phone": _phone(rng),
        "profile_url": f"{profile_base}/{name.lower().replace(' ', '-')}",
    }


def build_current(firm: dict) -> list[dict]:
    """Assemble the baseline roster for a firm: real partners + decoys."""
    slug = firm["slug"]
    rng = _rng(slug)
    offices = firm["options"]["offices"] or ["New York"]
    profile_base = f"https://{slug}.example.invalid/people"
    people = [_person(rng, slug, profile_base, offices) for _ in range(rng.randint(6, 9))]
    people += [_decoy(rng, slug, profile_base, offices) for _ in range(3)]
    return people


def build_next(current: list[dict], firm: dict) -> list[dict]:
    """Evolve a baseline roster into the 'next week' snapshot.

    Applies exactly one of each interesting change so the change feed demo is
    predictable: a joiner, a leaver, a promotion, an office move and a practice
    change. Deterministic per firm.
    """
    slug = firm["slug"]
    rng = _rng(slug + ":next")
    offices = firm["options"]["offices"] or ["New York"]
    profile_base = f"https://{slug}.example.invalid/people"

    next_people = [dict(p) for p in current]

    # 1) Change the first partner's title (guaranteed different, so the diff fires).
    if next_people:
        current_title = next_people[0]["title"]
        next_people[0]["title"] = "Senior Partner" if current_title == "Managing Partner" else "Managing Partner"

    # 2) Move the second partner to a different office (only possible if the firm
    #    has more than one office — e.g. Wachtell is New York-only).
    if len(next_people) > 1 and len(offices) > 1:
        current_office = next_people[1]["offices"][0]
        options = [o for o in offices if o != current_office] or [current_office]
        next_people[1]["offices"] = [rng.choice(options)]

    # 3) Add a practice area to the third partner (guaranteed to change the
    #    canonicalised set, so the diff fires).
    if len(next_people) > 2:
        existing = set(canonicalize_practices(next_people[2]["practices"]))
        for candidate in CORPORATE_PRACTICES:
            group = canonical_practice(candidate)
            if group and group not in existing:
                next_people[2]["practices"] = next_people[2]["practices"] + [candidate]
                break

    # 4) Give the fourth partner a new direct dial (contact change).
    if len(next_people) > 3:
        next_people[3]["phone"] = _phone(rng)

    # 5) Remove the fifth partner (they left the firm).
    if len(next_people) > 4:
        next_people.pop(4)

    # 6) Add a brand-new partner (they joined).
    next_people.append(_person(rng, slug, profile_base, offices))

    return next_people


def main() -> None:
    """Generate fixtures/current and fixtures/next for every configured firm."""
    firms = json.loads((ROOT / "config" / "firms.json").read_text(encoding="utf-8"))["firms"]
    (FIXTURES / "current").mkdir(parents=True, exist_ok=True)
    (FIXTURES / "next").mkdir(parents=True, exist_ok=True)

    for firm in firms:
        current = build_current(firm)
        (FIXTURES / "current" / f"{firm['slug']}.json").write_text(
            json.dumps(current, indent=2), encoding="utf-8"
        )
        nxt = build_next(current, firm)
        (FIXTURES / "next" / f"{firm['slug']}.json").write_text(
            json.dumps(nxt, indent=2), encoding="utf-8"
        )
        print(f"{firm['slug']:16} current={len(current):3d}  next={len(nxt):3d}")


if __name__ == "__main__":
    main()
