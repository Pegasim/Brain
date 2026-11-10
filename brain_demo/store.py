import json
import os
import re
import runpy
import sqlite3
import tempfile
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple, TypedDict

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
DEFAULT_DB = ROOT / "data" / "brain-demo.sqlite"
EXTRACTOR = ROOT / "third_party/second-brain/tools/extract/brain_extract.py"
RESULT_LIMIT = 12
EXCERPT_LIMIT = 600
APPLICATION_ID = 0x42524149  # BRAI

SCHEMA = """
CREATE TABLE record (
    ref TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    local_date TEXT NOT NULL,
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL,
    evidence TEXT REFERENCES record(ref)
);
CREATE VIRTUAL TABLE record_search USING fts5(title, text);
"""


class Record(NamedTuple):
    ref: str
    kind: str
    timestamp: str
    local_date: str
    title: str
    text: str
    source: str
    evidence: str | None


class LookupResult(TypedDict):
    evidence: list[dict]
    missing_references: list[str]


def record(ref, kind, timestamp, title, text, source, evidence=None):
    when = datetime.fromisoformat(timestamp)
    if when.utcoffset() is None:
        raise ValueError("Fixture timestamps must include an offset")
    return Record(ref, kind, when.isoformat(), when.date().isoformat(), title, text, source, evidence)


def fixture_records():
    calendar = FIXTURES / "demo-calendar.json"
    for event in json.loads(calendar.read_text(encoding="utf-8")):
        yield record(
            f"calendar:{event['id']}", "calendar_plan", event["start"],
            event["title"], event["description"], f"fixtures/{calendar.name}",
        )

    trace = FIXTURES / "demo-session.jsonl"
    extractor = runpy.run_path(str(EXTRACTOR))
    session = extractor["parse_codex"](trace)
    if session is None or session.started is None:
        raise ValueError("Synthetic session could not be extracted")
    # A user statement is evidence of what the owner reported. Assistant text
    # is excluded so it cannot accidentally be presented as corroboration.
    user_text = "\n\n".join(turn.text for turn in session.turns if turn.role == "user" and turn.text)
    if not user_text:
        raise ValueError("Synthetic session contains no user statements")
    text, _ = extractor["redact"](user_text)
    yield record(
        f"session:{session.sid}", "user_statement", session.started.isoformat(),
        "Demo review update", text, f"fixtures/{trace.name}",
    )

    note = FIXTURES / "demo-decision.md"
    header, body = note.read_text(encoding="utf-8").removeprefix("---\n").split("\n---\n", 1)
    entry = json.loads(header)
    yield record(
        f"note:{entry['id']}", "curated_decision", entry["recorded"], entry["title"],
        body.strip(), f"fixtures/{note.name}", entry["evidence"],
    )


def load_demo(path: Path = DEFAULT_DB) -> None:
    """Build a complete synthetic snapshot and publish it after validation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as existing:
                is_demo = existing.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        except sqlite3.DatabaseError:
            is_demo = False
        if not is_demo:
            raise ValueError(f"Refusing to replace a non-demo file: {path}")
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".sqlite", delete=False) as temporary:
        stage = Path(temporary.name)
    try:
        with closing(sqlite3.connect(stage)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            conn.executescript(SCHEMA)
            with conn:
                conn.executemany("INSERT INTO record VALUES (?, ?, ?, ?, ?, ?, ?, ?)", fixture_records())
                conn.execute("INSERT INTO record_search(rowid, title, text) SELECT rowid, title, text FROM record")
        os.replace(stage, path)
    finally:
        stage.unlink(missing_ok=True)


def evidence(row: sqlite3.Row) -> dict:
    return {
        "reference": row["ref"],
        "kind": row["kind"],
        "timestamp": row["timestamp"],
        "title": row["title"],
        "excerpt": row["text"][:EXCERPT_LIMIT],
        "source": row["source"],
        "supports": [row["evidence"]] if row["evidence"] else [],
    }


def lookup(path: Path, topic: str, start: str | None = None, end: str | None = None) -> LookupResult:
    terms = re.findall(r"[^\W_]+", topic[:200])
    if not terms:
        raise ValueError("Give a topic such as 'demo review'")
    first = date.fromisoformat(start) if start is not None else date.min
    last = date.fromisoformat(end) if end is not None else date.max
    if first > last:
        raise ValueError("The start date must be on or before the end date")
    query = " AND ".join(f'"{term}"' for term in terms)
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        # Hold a single read snapshot while resolving citations.
        conn.execute("BEGIN")
        rows = conn.execute(
            """SELECT r.* FROM record_search
               JOIN record AS r ON r.rowid = record_search.rowid
               WHERE record_search MATCH ? AND r.local_date BETWEEN ? AND ?
               ORDER BY rank, r.ref LIMIT ?""",
            (query, first.isoformat(), last.isoformat(), RESULT_LIMIT),
        ).fetchall()
        results = {row["ref"]: evidence(row) for row in rows}
        missing = []
        for row in rows:
            ref = row["evidence"]
            if ref and ref not in results:
                linked = conn.execute("SELECT * FROM record WHERE ref = ?", (ref,)).fetchone()
                if linked:
                    results[ref] = evidence(linked)
                elif ref not in missing:
                    missing.append(ref)
    return {"evidence": list(results.values()), "missing_references": missing}
