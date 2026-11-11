"""Temel arama: FTS5 tam metin + zaman gruplaması.

"Ne zaman X ile uğraşmıştım?" → `weeks()` eşleşmeleri ISO haftalarına toplar, en yoğun
dönemleri ve örnek satırları döndürür. Anlamsal (vektör) arama sonraki katman: aynı
event id'leri üzerinde çalışacak, bu fonksiyonlarla birleştirilecek (hibrit).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from collections import Counter, defaultdict

from cfv.db import fold_tr

_TOKEN = re.compile(r"[\w]+", re.UNICODE)


def fts_query(text: str) -> str:
    """Serbest metni güvenli FTS5 sorgusuna çevir: her kelime önek eşleşmesi, VE ile.
    Türkçe ekler için önek: 'ödevlerimi' yerine 'ödev' yazmak daha çok bulur.
    Tırnak içindeki ifadeler aynen aranır."""
    phrases = re.findall(r'"([^"]+)"', text)
    rest = re.sub(r'"[^"]+"', " ", text)
    parts = [f'"{fold_tr(p)}"' for p in phrases]
    parts += [f'"{fold_tr(t)}"*' for t in _TOKEN.findall(rest)]
    return " ".join(parts)


def _snippet(text: str | None, query: str, width: int = 140) -> str:
    if not text:
        return ""
    flat = text.replace("\n", " ")
    low = fold_tr(flat).lower()
    pos = -1
    for t in _TOKEN.findall(fold_tr(query).lower()):
        pos = low.find(t)
        if pos >= 0:
            break
    if pos < 0 or len(flat) <= width:
        return flat[:width]
    a = max(0, pos - width // 3)
    return ("…" if a else "") + flat[a : a + width] + ("…" if a + width < len(flat) else "")


def search(conn: sqlite3.Connection, query: str, *, kinds: list[str] | None = None,
           start: str | None = None, end: str | None = None, chat: str | None = None,
           person: str | None = None, limit: int = 20, order: str = "rank") -> list[dict]:
    """FTS eşleşmeleri. start/end: 'YYYY-MM-DD' (yerel gün, dahil). order: rank | time.
    chat: WhatsApp sohbet adı (içerir). person: kişi adı (içerir; foto/mesaj bağlantısı)."""
    q = fts_query(query)
    if not q:
        return []
    where, args = ["event_fts MATCH ?"], [q]
    if chat:
        where.append("e.kind = 'whatsapp' AND e.title LIKE ?")
        args.append(f"%{chat}%")
    if person:
        where.append("""e.id IN (SELECT ep.event_id FROM event_person ep JOIN person p ON p.id = ep.person_id
                         WHERE p.display_name LIKE ?)""")
        args.append(f"%{person}%")
    if kinds:
        where.append(f"e.kind IN ({','.join('?' * len(kinds))})")
        args += kinds
    if start:
        where.append("e.local_date >= ?")
        args.append(start)
    if end:
        where.append("e.local_date <= ?")
        args.append(end)
    order_sql = "bm25(event_fts)" if order == "rank" else "e.start_utc"
    rows = conn.execute(f"""
        SELECT e.id, e.kind, e.local_date, e.start_utc, e.title, e.body, e.payload, s.kind
        FROM event_fts JOIN event e ON e.id = event_fts.rowid JOIN source s ON s.id = e.source_id
        WHERE {' AND '.join(where)} ORDER BY {order_sql} LIMIT ?""", (*args, limit)).fetchall()
    out = []
    for r in rows:
        text = r[5]
        out.append({"id": r[0], "kind": r[1], "local_date": r[2], "start_utc": r[3],
                    "title": r[4], "snippet": _snippet(text or r[4], query), "source": r[7],
                    "payload": json.loads(r[6]) if r[6] else None})
    return out


def weeks(conn: sqlite3.Connection, query: str, *, kinds: list[str] | None = None,
          limit: int = 10, samples: int = 3) -> list[dict]:
    """Eşleşmeleri ISO haftalarına grupla; en çok eşleşen haftalar önce."""
    hits = search(conn, query, kinds=kinds, limit=5000, order="rank")
    groups: dict[str, list[dict]] = defaultdict(list)
    for h in hits:
        y, w, _ = dt.date.fromisoformat(h["local_date"]).isocalendar()
        groups[f"{y}-W{w:02d}"].append(h)
    ranked = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:limit]
    return [{"week": wk, "hits": len(hs), "first": min(h["local_date"] for h in hs),
             "last": max(h["local_date"] for h in hs),
             "kinds": dict(Counter(h["kind"] for h in hs)),
             "samples": [{k: h[k] for k in ("local_date", "kind", "title", "snippet")} for h in hs[:samples]]}
            for wk, hs in ranked]


def timeline(conn: sqlite3.Connection, start: str, end: str, *, kinds: list[str] | None = None,
             limit: int = 500) -> list[dict]:
    """Tarih aralığındaki event'ler (yerel gün, dahil), zaman sırasıyla."""
    where, args = ["local_date BETWEEN ? AND ?"], [start, end]
    if kinds:
        where.append(f"kind IN ({','.join('?' * len(kinds))})")
        args += kinds
    rows = conn.execute(f"""SELECT id, kind, local_date, start_utc, end_utc, title, body, payload
                            FROM event WHERE {' AND '.join(where)} ORDER BY start_utc LIMIT ?""",
                        (*args, limit)).fetchall()
    return [{"id": r[0], "kind": r[1], "local_date": r[2], "start_utc": r[3], "end_utc": r[4],
             "title": r[5], "body": (r[6] or "")[:500], "payload": json.loads(r[7]) if r[7] else None}
            for r in rows]


def digest(conn: sqlite3.Connection, start: str, end: str) -> dict:
    """Dönem özeti: sayımlar, takvim, ödevler, fotoğraftaki kişiler, en çok yazışılanlar, sağlık."""
    c = conn.execute
    counts = dict(c("SELECT kind, count(*) FROM event WHERE local_date BETWEEN ? AND ? GROUP BY kind",
                    (start, end)).fetchall())
    people = c("""SELECT p.display_name, count(*) FROM event e JOIN event_person ep ON ep.event_id=e.id
                  JOIN person p ON p.id=ep.person_id WHERE e.local_date BETWEEN ? AND ?
                  AND ep.role='in_photo' GROUP BY p.id ORDER BY 2 DESC LIMIT 15""", (start, end)).fetchall()
    chats = c("""SELECT title, count(*) FROM event WHERE kind='whatsapp' AND local_date BETWEEN ? AND ?
                 GROUP BY title ORDER BY 2 DESC LIMIT 10""", (start, end)).fetchall()
    places = c("""SELECT json_extract(payload,'$.moment') m, count(*) FROM event
                  WHERE kind IN ('photo','video') AND local_date BETWEEN ? AND ? AND m IS NOT NULL
                  GROUP BY m ORDER BY 2 DESC LIMIT 10""", (start, end)).fetchall()
    cal = c("""SELECT local_date, title FROM event WHERE kind='calendar' AND local_date BETWEEN ? AND ?
               ORDER BY start_utc LIMIT 100""", (start, end)).fetchall()
    todos = c("""SELECT local_date, title, json_extract(payload,'$.done') FROM event
                 WHERE kind='todo' AND local_date BETWEEN ? AND ? ORDER BY local_date LIMIT 300""",
              (start, end)).fetchall()
    health = c("""SELECT avg(json_extract(payload,'$.steps')), count(*) FROM event
                  WHERE kind='zepp_day' AND local_date BETWEEN ? AND ?""", (start, end)).fetchone()
    sleep = c("""SELECT avg(json_extract(payload,'$.asleep_min')) FROM event
                 WHERE kind='sleep' AND local_date BETWEEN ? AND ?""", (start, end)).fetchone()
    return {"start": start, "end": end, "counts": counts,
            "photo_people": people, "places": places, "top_chats": chats,
            "calendar": cal, "todos": [{"date": d, "text": t, "done": bool(x) if x is not None else None}
                                       for d, t, x in todos],
            "health": {"avg_steps": round(health[0]) if health[0] else None, "days": health[1],
                       "avg_sleep_h": round(sleep[0] / 60, 1) if sleep[0] else None}}
