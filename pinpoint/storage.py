"""SQLite persistence.

Design choices worth defending:

* **SQLite, standard library.** Zero-ops, single-file, transactional and more
  than enough for 15 firms (or 500). The storage class is a thin façade; swapping
  in Postgres later means reimplementing this one module, not touching the rest.
* **Point-in-time snapshots.** Every run writes a *snapshot* plus the full roster
  it observed (``roster_entries``). ``persons`` holds the latest state for fast
  reads; ``roster_entries`` is the immutable history that answers "who was there
  on date X".
* **An explicit change log.** ``change_events`` is the product feature ("what
  changed"), derived once at write time so the UI can page through it cheaply.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .models import (
    CHANGE_COVERAGE_DROPPED,
    CHANGE_SOURCE_CHANGED,
    CHANGE_SOURCE_DEGRADED,
    ChangeEvent,
    Firm,
    FirmSyncResult,
    Person,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS firms (
    slug         TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    homepage     TEXT,
    partners_url TEXT,
    adapter      TEXT,
    practice_url TEXT,
    coverage_gap TEXT
);

CREATE TABLE IF NOT EXISTS persons (
    person_key  TEXT PRIMARY KEY,
    firm_slug   TEXT NOT NULL,
    full_name   TEXT NOT NULL,
    title       TEXT,
    practices   TEXT,          -- JSON array
    offices     TEXT,          -- JSON array
    email       TEXT,
    phone       TEXT,
    profile_url TEXT,
    provenance  TEXT,
    source_id   TEXT,
    status      TEXT NOT NULL DEFAULT 'active',   -- 'active' | 'removed'
    first_seen  TEXT,
    last_seen   TEXT
);
CREATE INDEX IF NOT EXISTS idx_persons_firm ON persons (firm_slug);
CREATE INDEX IF NOT EXISTS idx_persons_status ON persons (status);

CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    finished_at TEXT,
    source      TEXT,
    firm_count  INTEGER,
    person_count INTEGER,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS roster_entries (
    snapshot_id INTEGER NOT NULL,
    person_key  TEXT NOT NULL,
    firm_slug   TEXT NOT NULL,
    full_name   TEXT,
    title       TEXT,
    practices   TEXT,
    offices     TEXT,
    email       TEXT,
    phone       TEXT,
    profile_url TEXT,
    provenance  TEXT,
    source_id   TEXT,
    PRIMARY KEY (snapshot_id, person_key)
);
CREATE INDEX IF NOT EXISTS idx_roster_snapshot ON roster_entries (snapshot_id);

CREATE TABLE IF NOT EXISTS firm_runs (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    firm_slug TEXT NOT NULL REFERENCES firms(slug),
    attempted_at TEXT NOT NULL,
    verified_at TEXT,
    source_url TEXT,
    source TEXT NOT NULL,
    health TEXT NOT NULL,
    reason TEXT NOT NULL,
    candidates INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, firm_slug)
);
CREATE INDEX IF NOT EXISTS idx_firm_runs_latest ON firm_runs (firm_slug, snapshot_id DESC);

CREATE TABLE IF NOT EXISTS change_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER,
    person_key  TEXT,
    firm_slug   TEXT,
    full_name   TEXT,
    change_type TEXT,
    field       TEXT,
    old_value   TEXT,
    new_value   TEXT,
    detected_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_changes_detected ON change_events (detected_at);
CREATE INDEX IF NOT EXISTS idx_changes_firm ON change_events (firm_slug);

-- Normalised facets. ``persons.practices``/``offices`` stay as JSON for cheap
-- row hydration, but filtering goes through these indexed tables so a practice
-- or office filter is an exact, indexable match instead of a LIKE scan.
CREATE TABLE IF NOT EXISTS person_practices (
    person_key TEXT NOT NULL,
    practice   TEXT NOT NULL,
    PRIMARY KEY (person_key, practice)
);
CREATE INDEX IF NOT EXISTS idx_pp_practice ON person_practices (practice);

CREATE TABLE IF NOT EXISTS person_offices (
    person_key TEXT NOT NULL,
    office     TEXT NOT NULL,
    PRIMARY KEY (person_key, office)
);
CREATE INDEX IF NOT EXISTS idx_po_office ON person_offices (office);
"""


class Store:
    """Thin, typed façade over a SQLite database."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False so a future threaded server can share it safely
        # (we serialise writes through short-lived transactions).
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._publishing = False
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.migrate()

    def migrate(self) -> None:
        """Create tables/indexes if they don't exist, then backfill facets."""
        self.conn.executescript(_SCHEMA)
        additions = (
            ("persons", "source_id"), ("roster_entries", "source_id"), ("firm_runs", "source_url"),
            ("firms", "practice_url"), ("firms", "coverage_gap"),
        )
        self.conn.execute("BEGIN IMMEDIATE")
        with self.conn:
            for table, column in additions:
                if column not in {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            self._backfill_facets()

    def _backfill_facets(self) -> None:
        """Rebuild the facet tables from ``persons`` JSON if they are empty.

        Older databases (created before the facet tables existed) have the JSON
        columns populated but the join tables empty; this makes filtering work
        without a manual migration step.
        """
        if self.conn.execute("SELECT COUNT(*) AS n FROM person_practices").fetchone()["n"]:
            return
        rows = list(self.conn.execute("SELECT person_key, practices, offices FROM persons"))
        if not rows:
            return
        prac, off = [], []
        for row in rows:
            prac.extend((row["person_key"], p) for p in _json_list(row["practices"]))
            off.extend((row["person_key"], o) for o in _json_list(row["offices"]))
        self.conn.executemany(
            "INSERT OR IGNORE INTO person_practices (person_key, practice) VALUES (?, ?)", prac
        )
        self.conn.executemany(
            "INSERT OR IGNORE INTO person_offices (person_key, office) VALUES (?, ?)", off
        )

    def close(self) -> None:
        """Close the underlying connection."""
        self.conn.close()

    @contextmanager
    def publish(self):
        """Publish one completed refresh, with rollback on interruption or error."""
        if self._publishing:
            raise RuntimeError("a publication is already in progress")
        self.conn.execute("BEGIN IMMEDIATE")
        self._publishing = True
        try:
            with self.conn:
                yield
        finally:
            self._publishing = False

    @contextmanager
    def read_view(self):
        """Execute related queries against a consistent SQLite snapshot."""
        self.conn.execute("BEGIN")
        with self.conn:
            yield

    def _commit(self) -> None:
        if not self._publishing:
            self.conn.commit()

    # -------------------------------------------------------------- firms
    def upsert_firm(self, firm: Firm) -> None:
        """Insert or update a firm row from config."""
        self.conn.execute(
            """
            INSERT INTO firms (slug, name, homepage, partners_url, adapter, practice_url, coverage_gap)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                name=excluded.name, homepage=excluded.homepage,
                partners_url=excluded.partners_url, adapter=excluded.adapter,
                practice_url=excluded.practice_url, coverage_gap=excluded.coverage_gap
            """,
            (firm.slug, firm.name, firm.homepage, firm.partners_url, firm.adapter,
             firm.options.get("practice_url"), firm.options.get("coverage_gap")),
        )
        self._commit()

    # -------------------------------------------------------------- persons
    def get_persons(self, firm_slug: str | None = None, status: str = "active") -> dict[str, Person]:
        """Return the latest state of people keyed by ``person_key``.

        Defaults to *active* people only, which is what change detection diffs
        against.
        """
        query = "SELECT * FROM persons"
        clauses, params = [], []
        if firm_slug:
            clauses.append("firm_slug = ?")
            params.append(firm_slug)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)

        out: dict[str, Person] = {}
        for row in self.conn.execute(query, params):
            out[row["person_key"]] = _row_to_person(row)
        return out

    def upsert_persons(self, people: list[Person], seen_at: str) -> None:
        """Insert/refresh people, marking them active and stamping ``last_seen``.

        ``first_seen`` is preserved on update via ``COALESCE``.
        """
        rows = []
        for p in people:
            rows.append(
                (
                    p.key,
                    p.firm_slug,
                    p.full_name,
                    p.title,
                    json.dumps(p.practices),
                    json.dumps(p.offices),
                    p.email,
                    p.phone,
                    p.profile_url,
                    p.provenance,
                    p.source_id,
                    "active",
                    seen_at,
                    seen_at,
                )
            )
        self.conn.executemany(
            """
            INSERT INTO persons (
                person_key, firm_slug, full_name, title, practices, offices,
                email, phone, profile_url, provenance, source_id, status, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(person_key) DO UPDATE SET
                full_name=excluded.full_name, title=excluded.title,
                practices=excluded.practices, offices=excluded.offices,
                email=excluded.email, phone=excluded.phone,
                profile_url=excluded.profile_url,
                provenance=excluded.provenance, source_id=COALESCE(excluded.source_id, persons.source_id),
                status='active',
                first_seen=COALESCE(persons.first_seen, excluded.first_seen),
                last_seen=excluded.last_seen
            """,
            rows,
        )
        self._replace_facets(people)
        self._commit()

    def _replace_facets(self, people: list[Person]) -> None:
        """Refresh the practice/office facet rows for ``people``.

        Deletes then re-inserts so a removed practice doesn't linger. No-op for an
        empty batch.
        """
        if not people:
            return
        keys = [(p.key,) for p in people]
        self.conn.executemany("DELETE FROM person_practices WHERE person_key=?", keys)
        self.conn.executemany("DELETE FROM person_offices WHERE person_key=?", keys)
        self.conn.executemany(
            "INSERT OR IGNORE INTO person_practices (person_key, practice) VALUES (?, ?)",
            [(p.key, practice) for p in people for practice in dict.fromkeys(p.practices)],
        )
        self.conn.executemany(
            "INSERT OR IGNORE INTO person_offices (person_key, office) VALUES (?, ?)",
            [(p.key, office) for p in people for office in dict.fromkeys(p.offices)],
        )

    def mark_removed(self, keys: list[str], at: str) -> None:
        """Mark people as removed (they disappeared from the live roster)."""
        if not keys:
            return
        self.conn.executemany(
            "UPDATE persons SET status='removed' WHERE person_key=?",
            [(key,) for key in keys],
        )
        self._commit()

    # -------------------------------------------------------------- snapshots
    def start_snapshot(self, started_at: str, source: str) -> int:
        """Open a new snapshot row and return its id."""
        cur = self.conn.execute(
            "INSERT INTO snapshots (started_at, source) VALUES (?, ?)", (started_at, source)
        )
        self._commit()
        return int(cur.lastrowid)

    def finish_snapshot(self, snapshot_id: int, finished_at: str, firm_count: int, person_count: int, notes: str = "") -> None:
        """Close a snapshot with its final counts."""
        self.conn.execute(
            """
            UPDATE snapshots
               SET finished_at=?, firm_count=?, person_count=?, notes=?
             WHERE id=?
            """,
            (finished_at, firm_count, person_count, notes, snapshot_id),
        )
        self._commit()

    def record_firm_run(self, snapshot_id: int, result: FirmSyncResult, at: str) -> None:
        """Record the attempt separately from the last verified observation."""
        self.conn.execute(
            """INSERT INTO firm_runs
               (snapshot_id, firm_slug, attempted_at, verified_at, source_url, source, health, reason, candidates)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (snapshot_id, result.firm_slug, at, result.verified_at, result.source_url, result.source,
             result.health, result.health_reason or result.error or "", result.fetched),
        )
        self._commit()

    def last_firm_source(self, firm_slug: str) -> str | None:
        row = self.conn.execute(
            """SELECT source FROM firm_runs
               WHERE firm_slug=? AND verified_at IS NOT NULL ORDER BY snapshot_id DESC LIMIT 1""",
            (firm_slug,),
        ).fetchone()
        return row["source"] if row else None

    def last_firm_verification(self, firm_slug: str) -> str | None:
        return self.conn.execute(
            "SELECT MAX(verified_at) FROM firm_runs WHERE firm_slug=?", (firm_slug,)
        ).fetchone()[0]

    def save_roster(self, snapshot_id: int, people: list[Person]) -> None:
        """Write the point-in-time roster for a snapshot."""
        self.conn.executemany(
            """
            INSERT INTO roster_entries (
                snapshot_id, person_key, firm_slug, full_name, title, practices,
                offices, email, phone, profile_url, provenance, source_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    snapshot_id, p.key, p.firm_slug, p.full_name, p.title,
                    json.dumps(p.practices), json.dumps(p.offices), p.email,
                    p.phone, p.profile_url, p.provenance, p.source_id,
                )
                for p in people
            ],
        )
        self._commit()

    def roster_at(self, snapshot_id: int) -> list[Person]:
        """Return the recorded roster for a snapshot (history view)."""
        rows = self.conn.execute(
            "SELECT * FROM roster_entries WHERE snapshot_id=?", (snapshot_id,)
        )
        return [_roster_row_to_person(r) for r in rows]

    def latest_snapshot_id(self) -> int | None:
        """Id of the most recent snapshot, or ``None``."""
        row = self.conn.execute("SELECT MAX(id) AS m FROM snapshots WHERE finished_at IS NOT NULL").fetchone()
        return row["m"] if row and row["m"] is not None else None

    def list_snapshots(self, limit: int = 100) -> list[dict]:
        """Snapshot history, newest first (for the ``snapshots`` command)."""
        rows = self.conn.execute(
            "SELECT id, started_at, finished_at, source, firm_count, person_count "
            "FROM snapshots WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]

    def snapshot_id_at_or_before(self, timestamp: str) -> int | None:
        """Id of the newest snapshot whose ``started_at`` is <= ``timestamp``."""
        row = self.conn.execute(
            """SELECT id AS m FROM snapshots
               WHERE finished_at IS NOT NULL AND julianday(started_at) <= julianday(?)
               ORDER BY julianday(started_at) DESC, id DESC LIMIT 1""", (timestamp,)
        ).fetchone()
        return row["m"] if row and row["m"] is not None else None

    # -------------------------------------------------------------- changes
    def add_events(self, events: list[ChangeEvent]) -> None:
        """Append change events to the log."""
        if not events:
            return
        self.conn.executemany(
            """
            INSERT INTO change_events (
                snapshot_id, person_key, firm_slug, full_name, change_type,
                field, old_value, new_value, detected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    e.snapshot_id, e.person_key, e.firm_slug, e.full_name,
                    e.change_type, e.field, e.old_value, e.new_value, e.detected_at,
                )
                for e in events
            ],
        )
        self._commit()

    def recent_changes(
        self,
        firm_slug: str | None = None,
        change_type: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """Return recent change events, newest first, with optional filters."""
        query = "SELECT * FROM change_events"
        clauses, params = [], []
        if firm_slug:
            clauses.append("firm_slug = ?")
            params.append(firm_slug)
        if change_type:
            clauses.append("change_type = ?")
            params.append(change_type)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self.conn.execute(query, params)]

    def page_changes(
        self,
        *,
        firm_slug: str | None = None,
        change_type: str | None = None,
        snapshot_id: int | None = None,
        person_key: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        """Paged, filtered change events, newest first, with a total count."""
        clauses, params = [], []
        if firm_slug:
            clauses.append("firm_slug = ?")
            params.append(firm_slug)
        if change_type:
            clauses.append("change_type = ?")
            params.append(change_type)
        if snapshot_id is not None:
            clauses.append("snapshot_id = ?")
            params.append(snapshot_id)
        if person_key:
            clauses.append("person_key = ?")
            params.append(person_key)
        if search:
            like = f"%{search}%"
            clauses.append("(full_name LIKE ? OR old_value LIKE ? OR new_value LIKE ?)")
            params += [like, like, like]
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        total = self.conn.execute(
            "SELECT COUNT(*) AS n FROM change_events" + where, params
        ).fetchone()["n"]
        rows = self.conn.execute(
            "SELECT * FROM change_events" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return int(total), [dict(row) for row in rows]

    def distinct_change_types(self) -> list[str]:
        """The change types present in the log, sorted (for the UI's filter)."""
        rows = self.conn.execute(
            "SELECT DISTINCT change_type FROM change_events ORDER BY change_type"
        )
        return [row["change_type"] for row in rows]

    def change_type_counts(self) -> dict[str, int]:
        """Count all change events by type (all-time figures)."""
        rows = self.conn.execute(
            "SELECT change_type, COUNT(*) AS n FROM change_events GROUP BY change_type"
        )
        return {row["change_type"]: row["n"] for row in rows}

    def change_type_counts_for_snapshot(self, snapshot_id: int) -> dict[str, int]:
        """Count change events belonging to exactly one snapshot.

        This is what the dashboard's "joined/left this refresh" chips use — an
        exact snapshot match, not a ``latest - 1`` guess.
        """
        rows = self.conn.execute(
            "SELECT change_type, COUNT(*) AS n FROM change_events WHERE snapshot_id = ? "
            "GROUP BY change_type",
            (snapshot_id,),
        )
        return {row["change_type"]: row["n"] for row in rows}

    def changes_for_snapshot(self, snapshot_id: int) -> list[dict]:
        """All change events for one snapshot, newest first."""
        rows = self.conn.execute(
            "SELECT * FROM change_events WHERE snapshot_id = ? ORDER BY id DESC",
            (snapshot_id,),
        )
        return [dict(row) for row in rows]

    def change_count(self) -> int:
        """Total number of change events ever logged."""
        row = self.conn.execute("SELECT COUNT(*) AS n FROM change_events").fetchone()
        return int(row["n"])

    # -------------------------------------------------------------- reporting
    def list_persons(
        self,
        firm_slug: str | None = None,
        practice: str | None = None,
        office: str | None = None,
        search: str | None = None,
        include_removed: bool = False,
    ) -> list[Person]:
        """Filtered people list for the dashboard and ``search`` command."""
        query = "SELECT * FROM persons"
        clauses, params = [], []
        if not include_removed:
            clauses.append("status='active'")
        if firm_slug:
            clauses.append("firm_slug = ?")
            params.append(firm_slug)
        if search:
            clauses.append("(full_name LIKE ? OR title LIKE ?)")
            params.extend([f"%{search}%"] * 2)
        if practice:
            clauses.append(
                "EXISTS (SELECT 1 FROM person_practices pp "
                "WHERE pp.person_key = persons.person_key AND pp.practice = ?)"
            )
            params.append(practice)
        if office:
            clauses.append(
                "EXISTS (SELECT 1 FROM person_offices po "
                "WHERE po.person_key = persons.person_key AND po.office = ?)"
            )
            params.append(office)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY full_name"
        return [_row_to_person(r) for r in self.conn.execute(query, params)]

    def page_persons(
        self,
        *,
        firm_slug: str | None = None,
        practice: str | None = None,
        office: str | None = None,
        search: str | None = None,
        sort: str = "name",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[Person]]:
        """Paged, faceted partner list with a total count (for the JSON API)."""
        clauses = ["p.status='active'"]
        params: list = []
        if firm_slug:
            clauses.append("p.firm_slug = ?")
            params.append(firm_slug)
        if search:
            clauses.append("(p.full_name LIKE ? OR p.title LIKE ?)")
            params.extend([f"%{search}%"] * 2)
        if practice:
            clauses.append(
                "EXISTS (SELECT 1 FROM person_practices pp "
                "WHERE pp.person_key = p.person_key AND pp.practice = ?)"
            )
            params.append(practice)
        if office:
            clauses.append(
                "EXISTS (SELECT 1 FROM person_offices po "
                "WHERE po.person_key = p.person_key AND po.office = ?)"
            )
            params.append(office)
        where = " WHERE " + " AND ".join(clauses)
        total = self.conn.execute(
            "SELECT COUNT(*) AS n FROM persons p" + where, params
        ).fetchone()["n"]
        order = "p.firm_slug, p.full_name, p.person_key" if sort == "firm" else "p.full_name, p.person_key"
        rows = self.conn.execute(
            f"SELECT p.* FROM persons p{where} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return int(total), [_row_to_person(row) for row in rows]

    def get_person(self, person_key: str) -> Person | None:
        """Return one person by key, or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM persons WHERE person_key=?", (person_key,)
        ).fetchone()
        return _row_to_person(row) if row else None

    def distinct_practices(self) -> list[str]:
        """The distinct practice groups in use, sorted (for the UI's facet)."""
        rows = self.conn.execute(
            """SELECT DISTINCT pp.practice FROM person_practices pp
               JOIN persons p USING(person_key) WHERE p.status='active' ORDER BY pp.practice"""
        )
        return [row["practice"] for row in rows]

    def distinct_offices(self) -> list[str]:
        """The distinct offices in use, sorted (for the UI's facet)."""
        rows = self.conn.execute(
            """SELECT DISTINCT po.office FROM person_offices po
               JOIN persons p USING(person_key) WHERE p.status='active' ORDER BY po.office"""
        )
        return [row["office"] for row in rows]

    def provenance_counts(self) -> dict[str, int]:
        """Active-partner counts by provenance, plus a ``total``."""
        rows = self.conn.execute(
            "SELECT provenance, COUNT(*) AS n FROM persons WHERE status='active' GROUP BY provenance"
        )
        counts = {row["provenance"] or "live": row["n"] for row in rows}
        counts["total"] = sum(counts.values())
        return counts

    def firm_stats(self) -> list[dict]:
        """Per-firm counts of active partners (the dashboard's firm table)."""
        rows = self.conn.execute(
            """
            SELECT f.slug AS slug, f.name AS name,
                   COUNT(CASE WHEN p.status='active' THEN 1 END) AS partners
              FROM firms f
              LEFT JOIN persons p ON p.firm_slug = f.slug
             GROUP BY f.slug
             ORDER BY f.name
            """
        )
        return [dict(row) for row in rows]

    def firm_health(self) -> list[dict]:
        """Per-firm coverage for the dashboard's firm table.

        Reports how many active partners each firm has and how many of those came
        from a live source, plus the most recent time the firm was refreshed.
        """
        rows = self.conn.execute(
            """
            SELECT f.slug AS slug, f.name AS name,
                   COUNT(CASE WHEN p.status='active' THEN 1 END) AS partners,
                   COUNT(CASE WHEN p.status='active' AND p.provenance='live' THEN 1 END) AS live,
                   COUNT(CASE WHEN p.status='active' AND p.provenance='reviewed' THEN 1 END) AS reviewed,
                   MAX(CASE WHEN p.status='active' THEN p.last_seen END) AS last_seen,
                   run.attempted_at AS last_attempt, run.health,
                   COALESCE(NULLIF(run.reason, ''), f.coverage_gap) AS reason,
                   run.source AS attempted_source,
                   COALESCE(run.source_url, f.practice_url) AS source_url, f.coverage_gap,
                   (SELECT MAX(verified_at) FROM firm_runs WHERE firm_slug=f.slug) AS last_verified
              FROM firms f
              LEFT JOIN persons p ON p.firm_slug = f.slug
              LEFT JOIN firm_runs run ON run.firm_slug = f.slug AND run.snapshot_id = (
                   SELECT MAX(snapshot_id) FROM firm_runs WHERE firm_slug=f.slug
              )
             GROUP BY f.slug
             ORDER BY f.name
            """
        )
        return [dict(row) for row in rows]

    def health_flags_for_snapshot(self, snapshot_id: int | None) -> dict[str, str]:
        """Map firm slug -> health event type for one snapshot.

        Lets the dashboard mark firms a run could not refresh from a trusted
        source, without re-deriving it from timestamps.
        """
        if snapshot_id is None:
            return {}
        rows = self.conn.execute(
            "SELECT firm_slug, change_type FROM change_events "
            "WHERE snapshot_id=? AND change_type IN (?, ?, ?)",
            (snapshot_id, CHANGE_SOURCE_DEGRADED, CHANGE_COVERAGE_DROPPED, CHANGE_SOURCE_CHANGED),
        )
        return {row["firm_slug"]: row["change_type"] for row in rows}

    def total_active(self) -> int:
        """Total number of active partners stored."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM persons WHERE status='active'"
        ).fetchone()
        return int(row["n"])


def _json_list(value: str | None) -> list[str]:
    """Decode a JSON array column, tolerating nulls/garbage."""
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    return decoded if isinstance(decoded, list) else []


def _row_to_person(row: sqlite3.Row) -> Person:
    """Map a ``persons`` row to a :class:`Person`."""
    return Person(
        key=row["person_key"],
        firm_slug=row["firm_slug"],
        full_name=row["full_name"],
        title=row["title"] or "",
        practices=_json_list(row["practices"]),
        offices=_json_list(row["offices"]),
        email=row["email"],
        phone=row["phone"],
        profile_url=row["profile_url"],
        provenance=row["provenance"] or "live",
        source_id=row["source_id"],
    )


def _roster_row_to_person(row: sqlite3.Row) -> Person:
    """Map a ``roster_entries`` row to a :class:`Person`."""
    return Person(
        key=row["person_key"],
        firm_slug=row["firm_slug"],
        full_name=row["full_name"],
        title=row["title"] or "",
        practices=_json_list(row["practices"]),
        offices=_json_list(row["offices"]),
        email=row["email"],
        phone=row["phone"],
        profile_url=row["profile_url"],
        provenance=row["provenance"] or "live",
        source_id=row["source_id"],
    )
