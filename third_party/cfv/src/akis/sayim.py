"""Ortak sayımlar: kaynak başına olay sayısı, son olay, foto türevleri (yalnız okur)."""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
from pathlib import Path

from cfv import db


def ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def counts(path: Path) -> dict[str, int]:
    """kaynak türü → olay sayısı (olayı olmayan tür de 0 ile görünür)."""
    with ro(path) as c:
        return dict(c.execute(
            "SELECT s.kind, count(e.id) FROM source s LEFT JOIN event e ON e.source_id=s.id GROUP BY s.kind"))


def alias_count(path: Path) -> int:
    """person_alias satırı (kaynaklar arası ortak; ingest silmez → düşüş = hata ya da DB yeniden kurulumu)."""
    with ro(path) as c:
        return c.execute("SELECT count(*) FROM person_alias").fetchone()[0]


def last_events(path: Path, now: dt.datetime | None = None) -> dict[str, str | None]:
    """kaynak türü → geleceğe tarihli olmayan en son olayın start_utc'si."""
    now_s = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with ro(path) as c:
        return dict(c.execute(
            "SELECT s.kind, max(CASE WHEN e.start_utc <= ? THEN e.start_utc END) "
            "FROM source s LEFT JOIN event e ON e.source_id=s.id GROUP BY s.kind", (now_s,)))


def sources(path: Path) -> list[tuple]:
    """(kind, raw_path, snapshot, imported_at)"""
    with ro(path) as c:
        return c.execute("SELECT kind, raw_path, snapshot, imported_at FROM source ORDER BY kind").fetchall()


def _n_files(d: Path, suffix: str) -> int:
    try:
        return sum(1 for e in os.scandir(d) if e.name.endswith(suffix))
    except FileNotFoundError:
        return 0


def photo_derivs() -> dict[str, int]:
    out = db.data_dir() / "processed" / "photos"
    n_vec = 0
    v = out / "vectors.sqlite"
    if v.exists():
        try:
            with ro(v) as c:
                n_vec = c.execute("SELECT count(*) FROM item").fetchone()[0]
        except sqlite3.Error:
            n_vec = -1
    return {"vektor": n_vec, "small": _n_files(out / "small", ".jpg"), "mid": _n_files(out / "mid", ".jpg"),
            "eval": _n_files(out / "eval", ".json")}


def eval_match(path: Path) -> tuple[int, int]:
    """(eval dosyası sayısı, iphone-photos olaylarında external_id'si bulunan)"""
    ev = db.data_dir() / "processed" / "photos" / "eval"
    uuids = [p.stem for p in ev.glob("*.json")]
    with ro(path) as c:
        have = {r[0] for r in c.execute(
            "SELECT e.external_id FROM event e JOIN source s ON s.id=e.source_id WHERE s.kind='iphone-photos'")}
    return len(uuids), sum(u in have for u in uuids)


def broken_symlinks(root: Path) -> int:
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames + dirnames:
            p = os.path.join(dirpath, name)
            if os.path.islink(p) and not os.path.exists(p):
                n += 1
    return n
