"""WhatsApp mesajları → sohbet oturumları.

Aynı sohbette GAP'ten uzun sessizlik yeni oturum başlatır. Sistem mesajları sayılmaz.
Kişiler: DM'de karşı taraf, grupta yazan (event_person.counterpart/sender).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

GAP_S = 30 * 60

QUERY = """
SELECT e.id, json_extract(e.payload,'$.chat_jid'), e.title, json_extract(e.payload,'$.chat_type'),
       e.start_utc, e.local_date, coalesce(json_extract(e.payload,'$.from_me'),0),
       (SELECT group_concat(ep.person_id) FROM event_person ep
         WHERE ep.event_id = e.id AND ep.role IN ('counterpart','sender'))
FROM event e
WHERE e.kind = 'whatsapp' AND coalesce(json_extract(e.payload,'$.type'),'') != 'system'
ORDER BY 2, e.start_utc, e.id
"""


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def build(conn: sqlite3.Connection, gap_s: int = GAP_S) -> list[dict]:
    out: list[dict] = []
    cur: dict | None = None
    last_t = 0.0
    for _id, jid, chat, ctype, start, day, me, people in conn.execute(QUERY):
        t = _ts(start)
        if cur is None or jid != cur["chat_jid"] or t - last_t > gap_s:
            cur = {"chat_jid": jid, "chat": chat, "chat_type": ctype, "day": day,
                   "start_utc": start, "end_utc": start, "n_msgs": 0, "n_me": 0, "people": set()}
            out.append(cur)
        cur["end_utc"] = start
        cur["n_msgs"] += 1
        cur["n_me"] += int(bool(me))
        if people:
            cur["people"].update(int(p) for p in people.split(","))
        last_t = t
    for s in out:
        s["people"] = sorted(s["people"])
    out.sort(key=lambda s: s["start_utc"])
    return out
