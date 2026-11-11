""""Gün kapandı" kaydı — gelecekteki tüketiciler (Haiku gün özeti, cfv-ruya) için bağlantı noktası.

data/processed/akis.sqlite (cfv.sqlite her tur baştan kurulduğu için ayrı dosya):
  gun(tarih PK, kapandi_at, imza, revizyon, son_degisim_at, kaynak_sayilari)
  gun_degisim(tarih, revizyon, at, eski_sayilar, yeni_sayilar)   -- yalnız ekleme
Başarılı her gece turunda dün (Europe/Istanbul) dahil tüm geçmiş günler için içerik imzası hesaplanır
(db.PRIVATE_KINDS — rüya — hariç):
yeni gün → revizyon 1 (gün kapandı); kapanmış günün imzası değiştiyse (geç gelen uyku/foto) revizyon+1.
Satır silinmez. Tüketici kendi "işlediğim revizyon"unu tutar, gun.revizyon > o olan günleri yeniden işler.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

from cfv import db

TZ = ZoneInfo("Europe/Istanbul")
# Rüya (cfv-ruya, sabah gelir) günün imzasına girmez: kapanışı/revizyonu tetiklemesin, sayısı da burada görünmesin.
EXCLUDE = tuple(db.PRIVATE_KINDS)

SCHEMA = """
CREATE TABLE IF NOT EXISTS gun (
    tarih           TEXT PRIMARY KEY,   -- yerel gün (YYYY-MM-DD)
    kapandi_at      TEXT NOT NULL,      -- ilk kapanış (UTC ISO)
    imza            TEXT NOT NULL,      -- o günün olay içeriğinin sha1'i
    revizyon        INTEGER NOT NULL,   -- 1 = ilk kapanış; geç veri geldikçe artar
    son_degisim_at  TEXT NOT NULL,
    kaynak_sayilari TEXT NOT NULL       -- {"whatsapp": 123, ...}
);
CREATE TABLE IF NOT EXISTS gun_degisim (
    tarih TEXT NOT NULL, revizyon INTEGER NOT NULL, at TEXT NOT NULL,
    eski_sayilar TEXT, yeni_sayilar TEXT NOT NULL
);
"""


def path() -> Path:
    return db.data_dir() / "processed" / "akis.sqlite"


def connect(p: Path | None = None) -> sqlite3.Connection:
    p = p or path()
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p)
    c.executescript(SCHEMA)
    return c


def day_signatures(cfv_db: Path, upto: str) -> dict[str, tuple[str, dict[str, int]]]:
    """yerel gün → (imza, kaynak sayıları); yalnız upto (dahil) ve öncesi. Olay kimlikleri her ingest'te değiştiği
    için imzaya girmez; içerik (external_id, zaman, başlık, gövde, payload) girer."""
    hashes: dict[str, "hashlib._Hash"] = {}
    cnt: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    c = sqlite3.connect(f"file:{cfv_db}?mode=ro", uri=True)
    try:
        cur = c.execute(
            "SELECT e.local_date, s.kind, e.external_id, e.start_utc, e.end_utc, e.title, e.body, e.payload "
            "FROM event e JOIN source s ON s.id=e.source_id "
            f"WHERE e.local_date <= ? AND s.kind NOT IN ({','.join('?' * len(EXCLUDE))}) AND e.kind NOT IN "
            f"({','.join('?' * len(EXCLUDE))}) ORDER BY e.local_date, s.kind, e.external_id, e.start_utc",
            (upto, *EXCLUDE, *EXCLUDE))
        for row in cur:
            d = row[0]
            h = hashes.get(d)
            if h is None:
                h = hashes[d] = hashlib.sha1()
            h.update("\x1f".join("" if x is None else str(x) for x in row[1:]).encode())
            h.update(b"\x1e")
            cnt[d][row[1]] += 1
    finally:
        c.close()
    return {d: (hashes[d].hexdigest(), dict(cnt[d])) for d in hashes}


def close_days(cfv_db: Path, now: dt.datetime | None = None, akis_db: Path | None = None) -> dict:
    """Dün dahil geçmiş günleri kapat / revize et. Döner: {'kapanan': [...], 'revize': [...], 'dun': tarih}."""
    now = now or dt.datetime.now(dt.timezone.utc)
    at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    yesterday = (now.astimezone(TZ).date() - dt.timedelta(days=1)).isoformat()
    sigs = day_signatures(cfv_db, yesterday)
    sigs.setdefault(yesterday, (hashlib.sha1().hexdigest(), {}))  # olaysız gün de kapanır
    closed, revised = [], []
    with connect(akis_db) as c:
        old = {r[0]: (r[1], r[2], r[3]) for r in c.execute("SELECT tarih, imza, revizyon, kaynak_sayilari FROM gun")}
        for d, (sig, n) in sorted(sigs.items()):
            nj = json.dumps(n, ensure_ascii=False, sort_keys=True)
            if d not in old:
                c.execute("INSERT INTO gun VALUES (?,?,?,?,?,?)", (d, at, sig, 1, at, nj))
                c.execute("INSERT INTO gun_degisim VALUES (?,?,?,?,?)", (d, 1, at, None, nj))
                closed.append(d)
            elif old[d][0] != sig:
                rev = old[d][1] + 1
                c.execute("UPDATE gun SET imza=?, revizyon=?, son_degisim_at=?, kaynak_sayilari=? WHERE tarih=?",
                          (sig, rev, at, nj, d))
                c.execute("INSERT INTO gun_degisim VALUES (?,?,?,?,?)", (d, rev, at, old[d][2], nj))
                revised.append(d)
        # önceden kapanmış ama artık hiç olayı olmayan gün: imzası boşa döner (revizyon olarak kaydedilir)
        for d in sorted(set(old) - set(sigs)):
            empty = hashlib.sha1().hexdigest()
            if old[d][0] != empty:
                rev = old[d][1] + 1
                c.execute("UPDATE gun SET imza=?, revizyon=?, son_degisim_at=?, kaynak_sayilari='{}' WHERE tarih=?",
                          (empty, rev, at, d))
                c.execute("INSERT INTO gun_degisim VALUES (?,?,?,?,?)", (d, rev, at, old[d][2], "{}"))
                revised.append(d)
    return {"dun": yesterday, "kapanan": closed, "revize": revised}


def summary(akis_db: Path | None = None, days: int = 7) -> dict:
    p = akis_db or path()
    if not p.exists():
        return {"son_kapanan": None, "revize_son": []}
    with sqlite3.connect(f"file:{p}?mode=ro", uri=True) as c:
        last = c.execute("SELECT max(tarih) FROM gun").fetchone()[0]
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rev = c.execute("SELECT tarih, revizyon FROM gun WHERE revizyon > 1 AND son_degisim_at >= ? "
                        "ORDER BY tarih", (since,)).fetchall()
    return {"son_kapanan": last, "revize_son": rev}
