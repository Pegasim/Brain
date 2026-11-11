"""Tuna'nın eşleşme kararları — cfv.sqlite'tan AYRI dosya (data/config/decisions.sqlite).

cfv.sqlite her ingest'te baştan üretilir; kararlar orada tutulursa kaybolur. Bu yüzden kararlar
person.id'ye değil kararlı anahtara, (source_kind, alias) çiftine bağlanır.
Satır hiç silinmez: geri alma `undone_at` doldurur. Geçerli karar = çiftin geri alınmamış son satırı.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import threading
from pathlib import Path

from cfv import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS decision (
    id        INTEGER PRIMARY KEY,
    at        TEXT NOT NULL,
    a_kind    TEXT NOT NULL, a_alias TEXT NOT NULL,
    b_kind    TEXT NOT NULL, b_alias TEXT NOT NULL,
    verdict   TEXT NOT NULL CHECK (verdict IN ('same', 'different')),
    undone_at TEXT,
    note      TEXT
);
CREATE INDEX IF NOT EXISTS decision_pair ON decision (a_kind, a_alias, b_kind, b_alias);
"""

Key = tuple[str, str]  # (source_kind, alias)


def default_path() -> Path:
    return db.data_dir() / "config" / "decisions.sqlite"


def ordered(a: Key, b: Key) -> tuple[Key, Key]:
    return (a, b) if a <= b else (b, a)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Decisions:
    def __init__(self, path: Path | None = None):
        self.path = path or default_path()
        self._lock = threading.Lock()
        self.version = 0  # her yazmada artar → model önbelleği yenilenir

    def _conn(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        conn = sqlite3.connect(self.path)
        if new:
            os.chmod(self.path, 0o600)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        return conn

    def all(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM decision ORDER BY id")]

    def effective(self) -> dict[tuple[Key, Key], dict]:
        """Çift → geçerli (geri alınmamış en son) karar."""
        out: dict[tuple[Key, Key], dict] = {}
        for r in self.all():
            if r["undone_at"]:
                continue
            out[((r["a_kind"], r["a_alias"]), (r["b_kind"], r["b_alias"]))] = r
        return out

    def add(self, a: Key, b: Key, verdict: str, note: str | None = None) -> int:
        if verdict not in ("same", "different"):
            raise ValueError("verdict")
        if a == b:
            raise ValueError("aynı takma ad")
        a, b = ordered(a, b)
        with self._lock, self._conn() as conn:
            # aynı çiftin önceki geçerli kararı geri alınmış sayılır (tek geçerli karar kalsın)
            now = _now()
            conn.execute(
                "UPDATE decision SET undone_at=? WHERE undone_at IS NULL AND a_kind=? AND a_alias=? "
                "AND b_kind=? AND b_alias=?", (now, *a, *b))
            rid = conn.execute(
                "INSERT INTO decision (at, a_kind, a_alias, b_kind, b_alias, verdict, note) "
                "VALUES (?,?,?,?,?,?,?)", (now, *a, *b, verdict, note)).lastrowid
            self.version += 1
            return rid

    def undo(self, decision_id: int) -> bool:
        with self._lock, self._conn() as conn:
            n = conn.execute("UPDATE decision SET undone_at=? WHERE id=? AND undone_at IS NULL",
                             (_now(), decision_id)).rowcount
            self.version += 1
            return n == 1
