"""Record when each Defter record was added, changed or removed, so an agent can ask what is new since a time.

Change times are refresh times, like an import log: a note committed at 10:00 and refreshed at 03:30 the next
night is new at 03:30. The first refresh has no earlier state, so it dates each record by its own time instead:
a note's last git commit when known, otherwise the record's timestamp. Those rows say "initial".
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TypedDict

SCHEMA = """
CREATE TABLE IF NOT EXISTS brain_change (
    ref TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT, hash TEXT,
    change TEXT NOT NULL CHECK (change IN ('initial', 'added', 'changed', 'removed')), changed_utc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS brain_change_time ON brain_change (changed_utc, ref);
"""
HIDDEN_KINDS = ("dream",)


class DeltaResult(TypedDict):
    changes: list[dict]
    has_more: bool
    next_cursor: str | None


def utc(moment: dt.datetime) -> str:
    """The event table's timestamp form, so change times and record times compare as text."""
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str) -> str:
    """ISO date or date-time; one without a timezone is read as UTC."""
    moment = dt.datetime.fromisoformat(value.strip())
    return utc(moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc))


def source_kinds(conn: sqlite3.Connection) -> set[str]:
    """Kinds of the sources that currently have records; refresh reads them before importing."""
    return {row[0] for row in conn.execute("SELECT DISTINCT s.kind FROM event e JOIN source s ON s.id=e.source_id")}


def record_changes(conn: sqlite3.Connection, now: dt.datetime, known_sources: set[str] = frozenset()):
    """Compare the refreshed records with the previous refresh. Called by refresh inside its staging transaction.

    Records of a source imported for the first time (every source on the first refresh, or for example a newly
    added export folder) are "initial" and keep their own dates, so old material does not show up as new.
    """
    stamp = utc(now)
    first = not conn.execute("SELECT 1 FROM brain_change LIMIT 1").fetchone()
    old = {row[0]: row[1:] for row in conn.execute("SELECT ref, kind, title, hash, change FROM brain_change")}
    rows, seen = [], set()
    records = conn.execute("SELECT r.ref, e.kind, e.title, e.body, e.start_utc, e.payload, s.kind FROM brain_reference r "
                           "JOIN event e ON e.id=r.event_id JOIN source s ON s.id=e.source_id")
    for ref, kind, title, body, start, payload, source in records:
        seen.add(ref)
        digest = hashlib.sha256("\0".join((title or "", body or "", start or "")).encode()).hexdigest()
        previous = old.get(ref)
        if previous is None and (first or source not in known_sources):
            rows.append((ref, kind, title, digest, "initial", json.loads(payload or "{}").get("committed_utc") or start))
        elif previous is None or previous[3] == "removed":
            rows.append((ref, kind, title, digest, "added", stamp))
        elif previous[2] != digest:
            rows.append((ref, kind, title, digest, "changed", stamp))
    for ref, (kind, title, _, change) in old.items():
        if ref not in seen and change != "removed":
            rows.append((ref, kind, title, None, "removed", stamp))
    conn.executemany("INSERT OR REPLACE INTO brain_change VALUES (?, ?, ?, ?, ?, ?)", rows)


def delta(path: Path, since: str | None = None, cursor: str | None = None, kinds: list[str] | None = None,
          limit: int = 50) -> DeltaResult:
    """Records added, changed or removed at or after `since`, oldest first; pass `next_cursor` back to continue."""
    if cursor:
        after, separator, after_ref = cursor.partition("|")
        if not separator or parse_time(after) != after:
            raise ValueError("Use the next_cursor value exactly as returned")
        where, args = ["(c.changed_utc > ? OR (c.changed_utc = ? AND c.ref > ?))"], [after, after, after_ref]
    elif since:
        cursor = f"{parse_time(since)}|"  # every ref sorts after "", so this cursor also means "at or after"
        where, args = ["c.changed_utc >= ?"], [parse_time(since)]
    else:
        raise ValueError("Pass since (an ISO date or time) or a cursor")
    where.append(f"c.kind NOT IN ({','.join('?' * len(HIDDEN_KINDS))})")
    args += HIDDEN_KINDS
    if kinds:
        where.append(f"c.kind IN ({','.join('?' * len(kinds))})")
        args += kinds
    limit = max(1, min(limit, 200))
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        if not conn.execute("SELECT name FROM sqlite_master WHERE name='brain_change'").fetchone():
            raise ValueError("No change log yet. Run refresh.")
        rows = conn.execute(
            "SELECT c.ref, c.kind, c.title, c.change, c.changed_utc, e.start_utc, e.payload FROM brain_change c "
            "LEFT JOIN brain_reference r ON r.ref=c.ref LEFT JOIN event e ON e.id=r.event_id "
            f"WHERE {' AND '.join(where)} ORDER BY c.changed_utc, c.ref LIMIT ?", (*args, limit + 1)).fetchall()
    changes = [{"reference": ref, "kind": kind, "change": change, "changed_at": changed, "title": title,
                "timestamp": start, "path": json.loads(payload or "{}").get("note_path")}
               for ref, kind, title, change, changed, start, payload in rows[:limit]]
    more = len(rows) > limit
    last = changes[-1] if changes else None
    return {"changes": changes, "has_more": more,
            "next_cursor": f"{last['changed_at']}|{last['reference']}" if last else cursor}
