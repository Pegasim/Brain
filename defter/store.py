"""Connect CFV's actual schema/search to the second brain's capture and filing."""
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import runpy
import sqlite3
import subprocess
import tempfile
from collections import Counter
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import TypedDict
from urllib.parse import quote, unquote

from cfv import db, search
from cfv.ingest import Batch, drop_other_snapshots
from cfv.sources import calendar_ics

from . import changes, recall
from .maintenance import ROOT, TOOLS, module, run

FIXTURES = ROOT / "fixtures"
DEFAULT_DB = ROOT / "data/processed/cfv.sqlite"
DEFAULT_VAULT = ROOT / "data/vault"
APPLICATION_ID = 0x42524149
EXCERPT_LIMIT = 600


class LookupResult(TypedDict):
    evidence: list[dict]
    missing_references: list[str]


def source(conn, kind: str, folder: Path, importer: str) -> Batch:
    """One directory per source: records an earlier refresh read from another path (a moved vault) are dropped."""
    batch = Batch(conn, kind, folder, importer)
    drop_other_snapshots(conn, kind, batch.source_id)
    return batch


def ingest_sessions(conn, folder: Path, source_name: str):
    extractor = runpy.run_path(str(TOOLS / "extract/brain_extract.py"))
    batch = source(conn, f"brain-sessions:{source_name}", folder, "brain-sessions@1")
    for path in sorted(folder.rglob("*.jsonl")):
        session = extractor["parse_codex"](path)
        if session is None:
            continue
        if session.started is None:
            raise ValueError(f"Session timestamp missing in {path.name}")
        text = "\n\n".join(t.text for t in session.turns if t.role == "user" and t.text)
        if not text:
            continue
        text, _ = extractor["redact"](text)
        batch.event(
            session.sid, "brain_session", session.started,
            title=text.splitlines()[0][:90], body=text,
            tz_offset_min=int(session.started.utcoffset().total_seconds() / 60),
            payload={"brain_ref": f"session:{source_name}:{session.sid}", "source_file": str(path.resolve())},
        )



def ingest_archive(conn, folder: Path, source_name: str):
    batch = source(conn, f"brain-archive:{source_name}", folder, "brain-archive@1")
    redact = module()["load_redactor"](folder)
    conversations = []
    for path in sorted(folder.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n") or "\n---\n" not in text:
            continue
        header, body = text[4:].split("\n---\n", 1)
        meta = {}
        for line in header.splitlines():
            key, separator, value = line.partition(": ")
            if separator:
                meta[key] = json.loads(value)
        if meta.get("type") == "conversation":
            conversations.append((path, meta, body))
    # Some harnesses reuse one session id for separate conversations; those are told apart by archive file.
    counts = Counter((meta["harness"], meta["session_id"]) for _, meta, _ in conversations)
    for path, meta, body in conversations:
        sid, harness = meta["session_id"], meta["harness"]
        identity = f"{harness}:{sid}" + (f":{path.stem}" if counts[harness, sid] > 1 else "")
        when = dt.datetime.fromisoformat(meta["started"])
        if when.utcoffset() is None:
            raise ValueError(f"Archive timestamp has no timezone: {path.name}")
        batch.event(
            identity, "brain_conversation", when,
            title=redact(meta["title"])[0], body=redact(body)[0],
            tz_offset_min=int(when.utcoffset().total_seconds() / 60),
            payload={"brain_ref": f"session:{source_name}:{identity}", "source_file": str(path.resolve())},
        )


def split_note(text: str) -> tuple[dict, str]:
    """Return a note's top-level frontmatter fields and body; a file without frontmatter is all body."""
    if text.startswith("---\n") and "\n---\n" in text[3:]:
        header, body = text[4:].split("\n---\n", 1)
        meta = {}
        for line in header.splitlines():
            key, separator, value = line.partition(": ")
            if separator and not key.startswith((" ", "-")):
                meta[key.strip()] = value.strip()
        return meta, body
    return {}, text


def git_dates(vault: Path) -> dict[str, dt.datetime]:
    """Last commit time of each file under notes/, keyed by vault-relative path; empty outside a git checkout."""
    try:
        log = subprocess.run(
            ["git", "-c", "core.quotePath=false", "-C", str(vault), "log", "--relative", "--format=%x00%cI",
             "--name-only", "--", "notes"], capture_output=True, text=True, check=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    dates, current = {}, None
    for line in log.splitlines():
        if line.startswith("\0"):
            current = dt.datetime.fromisoformat(line[1:])
        elif line and current:
            dates.setdefault(line, current)  # newest commit comes first
    return dates


def note_day(meta: dict, path: Path, committed: dt.datetime | None = None) -> tuple[date, str]:
    """Use the note's own date, then its last update, then its last git commit, then the file's modification day."""
    for key in ("date", "updated"):
        value = meta.get(key, "").strip("\"'")
        try:
            return date.fromisoformat(value[:10]), key
        except ValueError:
            continue
    if committed:
        return committed.date(), "git"
    return dt.datetime.fromtimestamp(path.stat().st_mtime).date(), "mtime"


def note_title(meta: dict, body: str, path: Path) -> str:
    title = meta.get("title", "")
    if title.startswith('"'):
        title = json.loads(title)
    if title:
        return title
    heading = next((line[2:].strip() for line in body.splitlines() if line.startswith("# ")), "")
    return heading or path.stem


def ingest_notes(conn, vault: Path):
    """Index every Markdown note: dated log entries by their id, curated pages by their path."""
    batch = source(conn, "brain-notes", vault, "brain-notes@1")
    redact = module()["load_redactor"](vault)
    root = vault / "notes"
    commits = git_dates(vault)
    for path in sorted(root.rglob("*.md")):
        meta, body = split_note(path.read_text(encoding="utf-8"))
        if not body.strip():
            continue
        committed = commits.get(path.relative_to(vault).as_posix())
        day, basis = note_day(meta, path, committed)
        rel = path.relative_to(root).with_suffix("").as_posix()
        if meta.get("id") and basis == "date":
            # Dated maintenance-log entry: its id is the stable reference other notes cite.
            external_id, ref = meta["id"], f"note:{meta['id']}"
        else:
            # Curated page (index, timeline, topic, personal): identified by its path in the vault.
            external_id, ref = f"path:{rel}", f"note:{quote(rel, safe='/')}"
        body, _ = redact(body)
        refs = re.findall(r"(?<!\w)(?:session|note|event):[A-Za-z0-9_.:%/@+-]+", body)
        batch.event(
            external_id, "brain_note", dt.datetime.combine(day, dt.time(), dt.timezone.utc),
            title=redact(note_title(meta, body, path))[0], body=body, day=day.isoformat(), time_quality="day",
            payload={"brain_ref": ref, "evidence_refs": refs, "date_basis": basis, "note_path": rel,
                     "source_file": str(path.resolve()),
                     **({"committed_utc": changes.utc(committed)} if committed else {})},
        )


def refresh(path: Path, vault: Path, calendars=(), sessions: Path | None = None, source_name="codex",
            archive: Path | None = None, now: dt.datetime | None = None):
    """Refresh selected sources in a staging copy, retaining all other CFV sources; log what changed at `now`."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", source_name):
        raise ValueError("Source name must be a short slug of letters, digits, '_' or '-'")
    path.parent.mkdir(parents=True, exist_ok=True)
    if sessions is not None and not sessions.is_dir():
        raise ValueError(f"Session directory does not exist: {sessions}")
    if archive is not None and not archive.is_dir():
        raise ValueError(f"Archive directory does not exist: {archive}")
    with open(path.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".sqlite", delete=False) as temporary:
            stage = Path(temporary.name)
        try:
            if path.exists():
                with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as old:
                    if not old.execute("SELECT name FROM sqlite_master WHERE name='event'").fetchone():
                        raise ValueError("Expected a CFV database. Rebuild the old synthetic demo with load.")
                    with closing(sqlite3.connect(stage)) as copy:
                        old.backup(copy)
            with closing(db.connect(stage)) as conn:
                conn.executescript(db.schema_sql())
                conn.executescript(recall.SCHEMA)
                conn.executescript(changes.SCHEMA)
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS brain_reference "
                    "(ref TEXT PRIMARY KEY, event_id INTEGER UNIQUE REFERENCES event(id) ON DELETE CASCADE)"
                )
                with conn:
                    if calendars:
                        calendar_ics.ingest(conn, list(calendars))
                    if sessions is not None:
                        ingest_sessions(conn, sessions, source_name)
                    if archive is not None:
                        ingest_archive(conn, archive, source_name)
                    ingest_notes(conn, vault)
                    conn.execute("INSERT OR REPLACE INTO meta VALUES ('brain_mode', 'private')")
                    conn.execute("DELETE FROM brain_reference")
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT e.*, s.kind AS source_kind, s.raw_path FROM event e "
                        "JOIN source s ON s.id=e.source_id"
                    )
                    refs = [(reference(row), row["id"]) for row in rows]
                    conn.executemany("INSERT INTO brain_reference VALUES (?, ?)", refs)
                    recall.index_passages(conn)
                    changes.record_changes(conn, now or dt.datetime.now(dt.timezone.utc))
                conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                if conn.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("Source references failed validation")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("PRAGMA journal_mode = DELETE")
            os.replace(stage, path)
        finally:
            stage.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(stage) + suffix).unlink(missing_ok=True)


def load_demo(path: Path = DEFAULT_DB):
    if path.exists():
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                marker = conn.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                old_demo = "record" in tables
                mode = conn.execute("SELECT value FROM meta WHERE key='brain_mode'").fetchone() if "meta" in tables else None
                is_demo = marker and (old_demo or mode == ("synthetic",))
        except sqlite3.DatabaseError:
            is_demo = False
        if not is_demo:
            raise ValueError(f"Refusing to replace a non-demo file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as temporary:
        temporary = Path(temporary)
        vault = temporary / "vault"
        entry, text = (FIXTURES / "demo-decision.md").read_text().removeprefix("---\n").split("\n---\n", 1)
        meta = json.loads(entry)
        code = run(vault, [
            "add", "--kind", "decision", "--topic", "demo", "--title", meta["title"],
            "--context", "The owner reported changing the demo review schedule.",
            "--result", text.strip(), "--evidence", "session:codex:demo-review-update",
            "--date", "2026-10-30", "--source", "synthetic-demo",
        ])
        if code or run(vault, ["consolidate"]):
            raise ValueError("Demo memory consolidation failed")
        refresh(temporary / "cfv.sqlite", vault, [FIXTURES / "demo-calendar.ics"], FIXTURES)
        # Source identity must survive the temporary workspace used by the demo.
        with closing(sqlite3.connect(temporary / "cfv.sqlite")) as conn:
            conn.execute("UPDATE source SET raw_path='synthetic-demo-vault' WHERE kind='brain-notes'")
            rows = conn.execute("SELECT id, payload FROM event WHERE kind='brain_note'").fetchall()
            for event_id, encoded in rows:
                payload = json.loads(encoded)
                payload["source_file"] = str(FIXTURES / "demo-decision.md")
                conn.execute("UPDATE event SET payload=? WHERE id=?", (json.dumps(payload), event_id))
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('brain_mode', 'synthetic')")
            conn.commit()
        os.replace(temporary / "cfv.sqlite", path)


def reference(row):
    payload = json.loads(row["payload"] or "{}")
    if payload.get("brain_ref"):
        return payload["brain_ref"]
    identity = f"{row['source_kind']}:{row['raw_path']}"
    namespace = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return f"event:{namespace}:{quote(row['external_id'], safe='')}"


def lookup(path: Path, topic: str, start: str | None = None, end: str | None = None) -> LookupResult:
    if not topic.strip() or len(topic) > 200:
        raise ValueError("Use a short search topic of 1 to 200 characters")
    if start:
        date.fromisoformat(start)
    if end:
        date.fromisoformat(end)
    if start and end and start > end:
        raise ValueError("The start date must be on or before the end date")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        hits = search.search(conn, topic, start=start, end=end, limit=12)
        conn.row_factory = sqlite3.Row
        results, missing = {}, []
        pending = []
        for hit in hits:
            row = conn.execute("SELECT ref FROM brain_reference WHERE event_id=?", (hit["id"],)).fetchone()
            if row:
                pending.append(row["ref"])
        for ref in pending:
            if ref in results or ref in missing:
                continue
            row = conn.execute(
                "SELECT e.*, s.raw_path FROM brain_reference r JOIN event e ON e.id=r.event_id "
                "JOIN source s ON s.id=e.source_id WHERE r.ref=? AND e.kind != 'dream'", (ref,)
            ).fetchone()
            if row is None:
                missing.append(ref)
                continue
            payload = json.loads(row["payload"] or "{}")
            refs = payload.get("evidence_refs", [])
            results[ref] = {
                "reference": ref, "kind": row["kind"], "timestamp": row["start_utc"],
                "title": row["title"], "excerpt": (row["body"] or "")[:EXCERPT_LIMIT],
                "source": payload.get("source_file") or row["raw_path"], "supports": refs,
            }
            if len(results) >= 48:
                break
            pending.extend(refs)
        return {"evidence": list(results.values()), "missing_references": missing}


class ReadResult(TypedDict):
    reference: str
    kind: str
    title: str | None
    timestamp: str
    path: str | None
    source: str | None
    supports: list[str]
    text: str
    offset: int
    total_chars: int
    has_more: bool


def read(path: Path, reference: str, offset: int = 0, limit: int = 20000) -> ReadResult:
    """A record's full stored text by reference, or a note by its vault path (`topics/bilun`, `notes/topics/bilun.md`)."""
    wanted = reference.strip()
    if not wanted or len(wanted) > 500:
        raise ValueError("Use a reference from recall, lookup or delta")
    offset, limit = max(0, offset), max(1, min(limit, 50000))
    refs = [wanted]
    if wanted.startswith("note:"):
        refs.append("note:" + quote(unquote(wanted[5:]), safe="/"))
    note_path = wanted.removeprefix("note:").removeprefix("notes/").removesuffix(".md")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        query = ("SELECT r.ref, e.kind, e.title, e.start_utc, e.body, e.payload, s.raw_path FROM brain_reference r "
                 "JOIN event e ON e.id=r.event_id JOIN source s ON s.id=e.source_id WHERE e.kind != 'dream' AND ")
        row = conn.execute(query + "r.ref IN (?, ?)", (refs[0], refs[-1])).fetchone() or conn.execute(
            query + "e.kind='brain_note' AND json_extract(e.payload, '$.note_path')=?", (unquote(note_path),)).fetchone()
    if row is None:
        raise ValueError(f"No record for {reference!r}. Use a reference from recall, lookup or delta.")
    payload, body = json.loads(row["payload"] or "{}"), row["body"] or ""
    return {"reference": row["ref"], "kind": row["kind"], "title": row["title"], "timestamp": row["start_utc"],
            "path": payload.get("note_path"), "source": payload.get("source_file") or row["raw_path"],
            "supports": payload.get("evidence_refs", []), "text": body[offset:offset + limit], "offset": offset,
            "total_chars": len(body), "has_more": offset + limit < len(body)}
