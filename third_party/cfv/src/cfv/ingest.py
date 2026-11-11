"""Ortak import katmanı.

Kural: bir kaynak her import edildiğinde o kaynağın event'leri SİLİNİP baştan yazılır
(ham veri tek gerçek kaynak, DB atılabilir). Kişiler (person) kaynaklar arası ortaktır,
silinmez; alias'lar üzerinden bulunur.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from pathlib import Path

from cfv import db
from cfv.timeutil import iso, local_date


def open_ro(path: Path) -> sqlite3.Connection:
    """Ham SQLite'ı salt-okur ve immutable aç (hardlink'li yedeği asla değiştirmez)."""
    if not path.exists():
        raise FileNotFoundError(path)
    wal = path.with_name(path.name + "-wal")
    if wal.exists() and wal.stat().st_size > 0:
        # immutable=1 WAL'ı yok sayar → son yazılanlar kaybolur. Ham veriye dokunmadan
        # işlemek için önce kopyalanmalı; sessizce eksik okumaktansa dur.
        raise RuntimeError(f"{path.name}: dolu WAL var ({wal.stat().st_size} B); kopyalayıp aç")
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def raw(*parts: str) -> Path:
    return db.data_dir().joinpath("raw", *parts)


# Yeni iPhone çıkarımları (cfv-iphone) tarihli klasöre yazılır ve bitince içine `.tamam` konur; yarım kopya
# okunmasın diye yalnız işaretli klasör kabul edilir. Bu tarihten önceki klasörler işaretsiz de geçerli (geriye uyum).
DONE_MARK = ".tamam"
MARK_SINCE = "2026-11-11"


def snapshot_date(d: Path) -> str:
    """'extract-2026-11-01' / 'photos-db-2026-11-01-2' → '2026-11-01'."""
    parts = d.name.split("-")
    for i in range(len(parts) - 2):
        if len(parts[i]) == 4 and parts[i].isdigit():
            return "-".join(parts[i:i + 3])
    return d.name


def latest_dir(prefix: str, legacy: str, *, root: Path | None = None) -> Path:
    """raw/iphone/<prefix>-<tarih>[-n] içinden en yeni tamamlanmış klasör; hiçbiri yoksa raw/iphone/<legacy>."""
    root = root or raw("iphone")
    ok = [d for d in sorted(root.glob(f"{prefix}-*")) if d.is_dir() and (
        (d / DONE_MARK).exists() or snapshot_date(d) < MARK_SINCE)]
    return ok[-1] if ok else root / legacy


def snapshot_of(path: Path) -> str | None:
    """Yolun içindeki ilk 'extract-<tarih>' / 'photos-db-<tarih>' atasının tarihi."""
    for d in (path, *path.parents):
        if re.match(r"^[a-z-]+-\d{4}-\d{2}-\d{2}", d.name):
            return snapshot_date(d)
    return None


def iphone_extract() -> Path:
    """En yeni tamamlanmış iPhone yedeği çıkarımı (raw/iphone/extract-<tarih>)."""
    return latest_dir("extract", "extract-2026-11-01")


def drop_other_snapshots(conn: sqlite3.Connection, kind: str, source_id: int) -> int:
    """Aynı türün eski snapshot'larından kalan olaylar (yeni snapshot aynı kayıtları getirir → çift olmasın)."""
    return conn.execute("DELETE FROM event WHERE source_id IN (SELECT id FROM source WHERE kind=? AND id<>?)",
                        (kind, source_id)).rowcount


class Batch:
    """Bir kaynağın tek import oturumu."""

    def __init__(self, conn: sqlite3.Connection, kind: str, raw_path: Path,
                 importer: str, snapshot: str | None = None):
        self.conn = conn
        self.kind = kind
        rel = str(raw_path.relative_to(db.data_dir())) if raw_path.is_relative_to(db.data_dir()) else str(raw_path)
        now = iso(dt.datetime.now(dt.timezone.utc))
        conn.execute(
            "INSERT INTO source (kind, raw_path, snapshot, imported_at, importer) VALUES (?,?,?,?,?) "
            "ON CONFLICT(kind, raw_path) DO UPDATE SET imported_at=excluded.imported_at, "
            "importer=excluded.importer, snapshot=excluded.snapshot",
            (kind, rel, snapshot, now, importer),
        )
        self.source_id = conn.execute(
            "SELECT id FROM source WHERE kind=? AND raw_path=?", (kind, rel)).fetchone()[0]
        conn.execute("DELETE FROM event WHERE source_id=?", (self.source_id,))
        self._person_cache: dict[tuple[str, str], int] = {}
        self.count = 0

    def event(self, external_id: str, kind: str, start: dt.datetime, *,
              end: dt.datetime | None = None, tz_offset_min: int | None = None,
              time_quality: str = "exact", title: str | None = None, body: str | None = None,
              lat: float | None = None, lon: float | None = None,
              payload: dict | None = None, day: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO event (source_id, external_id, kind, start_utc, end_utc, tz_offset_min, "
            "local_date, time_quality, title, body, lat, lon, payload) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.source_id, external_id, kind, iso(start), iso(end) if end else None,
             tz_offset_min, day or local_date(start, tz_offset_min), time_quality, title, body,
             lat, lon,
             json.dumps(payload, ensure_ascii=False, default=str) if payload else None),
        )
        self.count += 1
        return cur.lastrowid

    def person(self, alias: str, name: str, source_kind: str | None = None) -> int:
        """Alias'a karşılık gelen kişiyi bul, yoksa oluştur."""
        sk = source_kind or self.kind
        key = (sk, alias)
        if key in self._person_cache:
            return self._person_cache[key]
        row = self.conn.execute(
            "SELECT person_id FROM person_alias WHERE source_kind=? AND alias=?", key).fetchone()
        if row:
            pid = row[0]
        else:
            pid = self.conn.execute(
                "INSERT INTO person (display_name) VALUES (?)", (name,)).lastrowid
            self.conn.execute(
                "INSERT INTO person_alias (person_id, source_kind, alias, name) VALUES (?,?,?,?)",
                (pid, sk, alias, name))
        self._person_cache[key] = pid
        return pid

    def link_person(self, event_id: int, person_id: int, role: str, confidence: float = 1.0):
        self.conn.execute(
            "INSERT OR IGNORE INTO event_person VALUES (?,?,?,?)",
            (event_id, person_id, role, confidence))

    def media(self, event_id: int, path: Path, role: str = "original", **kw):
        self.conn.execute(
            "INSERT OR REPLACE INTO media (event_id, path, role, original_filename, mime, width, "
            "height, duration_s) VALUES (?,?,?,?,?,?,?,?)",
            (event_id, str(path), role, kw.get("original_filename"), kw.get("mime"),
             kw.get("width"), kw.get("height"), kw.get("duration_s")))

    def tag(self, event_id: int, *tags: str):
        self.conn.executemany("INSERT OR IGNORE INTO tag VALUES (?,?)",
                              [(event_id, t) for t in tags if t])
