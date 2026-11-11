"""Günlük hedef tablosu: her yaklaşım aynı alanları tahmin eder.

Kaynağın kapsamadığı günlerde hedef NULL'dır (0 değil): ör. saat takılmadıysa adım yok.
Kapsam: `covered_months` (aylık eşik; dışa aktarma boşlukları NULL).
Son gün: en yeni WhatsApp mesajının gününden bir önceki gün (son gün yarım olabilir).
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date, timedelta

COUNTS = ["msgs_sent", "msgs_recv", "sessions", "sessions_me", "photos", "screenshots",
          "todo_done", "todo_open", "cal_events", "cal_hours", "steps", "sleep_min"]
SETS = ["people", "chats", "projects"]
TEXT = ["summary"]

# hedef → kaynak grubu (kapsam bu gruba göre belirlenir)
GROUP = {
    "msgs_sent": "whatsapp", "msgs_recv": "whatsapp", "sessions": "whatsapp",
    "sessions_me": "whatsapp", "people": "whatsapp", "chats": "whatsapp",
    "photos": "photos", "screenshots": "photos",
    "todo_done": "todo", "todo_open": "todo",
    "cal_events": "calendar", "cal_hours": "calendar",
    "steps": "zepp", "sleep_min": "zepp_sleep",
    "projects": "report", "summary": "report",
}
# zepp/zepp_sleep/report gün gün var/yok: o gün kaydı yoksa NULL
RUN = 3  # kapsam başlangıcı: art arda RUN uygun ay
# ay başına (en az olay, en az aktif gün)
MIN = {"whatsapp": (300, 15), "photos": (30, 0), "todo": (1, 8), "calendar": (10, 0)}
WHERE = {"whatsapp": "kind='whatsapp'", "photos": "kind IN ('photo','video')",
         "todo": "kind='todo' AND time_quality='day'", "calendar": "kind='calendar'"}

TZ = "+3 hours"  # Türkiye; yurtdışı günlerde uyku/takvim saat sınırı kayabilir (bilinen sınır)


def days_between(a: str, b: str) -> list[str]:
    d0, d1 = date.fromisoformat(a), date.fromisoformat(b)
    return [(d0 + timedelta(n)).isoformat() for n in range((d1 - d0).days + 1)]


def covered_months(conn: sqlite3.Connection, group: str, last: str) -> set[str]:
    """Kaynağın kapsadığı aylar. Ay, en az MIN[g] olay ve MIN[g] aktif gün içermeli (son yarım ayda
    orantılı). Başlangıç: art arda RUN uygun ayın ilki; sonrasında her ay ayrı yargılanır (dışa aktarma
    boşlukları NULL olur, ör. WhatsApp'ın eksik ayları)."""
    n_ev, n_days = MIN[group]
    rows = conn.execute(f"SELECT substr(local_date,1,7) m, count(*), count(DISTINCT local_date) FROM event "
                        f"WHERE {WHERE[group]} AND local_date <= ? GROUP BY m ORDER BY m", (last,)).fetchall()
    ok = []
    for m, ev, days in rows:
        frac = min(1.0, int(last[8:]) / 30) if m == last[:7] else 1.0
        ok.append((m, ev >= n_ev * frac and days >= n_days * frac))
    for i in range(len(ok) - RUN + 1):
        if all(x for _, x in ok[i:i + RUN]):
            return {m for m, x in ok[i:] if x}
    return set()


def last_full_day(conn: sqlite3.Connection) -> str:
    (d,) = conn.execute("SELECT max(local_date) FROM event WHERE kind='whatsapp'").fetchone()
    return (date.fromisoformat(d) - timedelta(1)).isoformat()


def _per_day(conn, sql, *args) -> dict[str, tuple]:
    return {r[0]: r[1:] for r in conn.execute(sql, args)}


def build(conn: sqlite3.Connection, sessions: list[dict], last: str | None = None) -> tuple[list[dict], dict]:
    last = last or last_full_day(conn)
    months = {g: covered_months(conn, g, last) for g in WHERE}
    first = min(min(m) for m in months.values() if m) + "-01"

    photos = _per_day(conn, """
        SELECT local_date,
          sum(NOT EXISTS (SELECT 1 FROM tag t WHERE t.event_id=e.id AND t.tag IN ('album:WhatsApp','screenshot'))),
          sum(EXISTS (SELECT 1 FROM tag t WHERE t.event_id=e.id AND t.tag='screenshot'))
        FROM event e WHERE kind IN ('photo','video') GROUP BY local_date""")
    todos = _per_day(conn, """
        SELECT local_date, sum(json_extract(payload,'$.done')=1), sum(coalesce(json_extract(payload,'$.done'),0)=0)
        FROM event WHERE kind='todo' AND time_quality='day' GROUP BY local_date""")
    cal = _per_day(conn, """
        SELECT local_date, count(*),
          sum(min(24.0, max(0.0, (julianday(end_utc)-julianday(start_utc))*24)))
        FROM event WHERE kind='calendar' AND coalesce(json_extract(payload,'$.all_day'),0)=0
          AND end_utc IS NOT NULL GROUP BY local_date""")
    steps = _per_day(conn, "SELECT local_date, max(json_extract(payload,'$.steps')) FROM event "
                           "WHERE kind='zepp_day' GROUP BY local_date")
    sleep = _per_day(conn, f"""
        SELECT date(end_utc, '{TZ}'),
          sum(coalesce(json_extract(payload,'$.asleep_min'), json_extract(payload,'$.total_min')))
        FROM event WHERE kind='sleep' AND end_utc IS NOT NULL GROUP BY 1""")
    reports = _per_day(conn, "SELECT local_date, json_extract(payload,'$.projects'), title FROM event "
                             "WHERE kind='day_report'")

    wa = defaultdict(lambda: {"sent": 0, "recv": 0, "sessions": 0, "sessions_me": 0,
                              "people": set(), "chats": set()})
    for s in sessions:
        w = wa[s["day"]]
        w["sent"] += s["n_me"]
        w["recv"] += s["n_msgs"] - s["n_me"]
        w["sessions"] += 1
        if s["n_me"]:
            w["sessions_me"] += 1
            w["chats"].add(s["chat_jid"])
            if s["chat_type"] == "dm":
                w["people"].update(str(p) for p in s["people"])

    rows = []
    for d in days_between(first, last):
        cov = {g: d[:7] in m for g, m in months.items()}
        w = wa.get(d)
        r = {"day": d, "dow": date.fromisoformat(d).isoweekday()}
        if cov["whatsapp"]:
            w = w or wa[d]
            r.update(msgs_sent=w["sent"], msgs_recv=w["recv"], sessions=w["sessions"],
                     sessions_me=w["sessions_me"], people=sorted(w["people"]), chats=sorted(w["chats"]))
        if cov["photos"]:
            p = photos.get(d, (0, 0))
            r.update(photos=p[0], screenshots=p[1])
        if cov["todo"]:
            t = todos.get(d, (0, 0))
            r.update(todo_done=t[0], todo_open=t[1])
        if cov["calendar"]:
            c = cal.get(d, (0, 0.0))
            r.update(cal_events=c[0], cal_hours=round(min(24.0, c[1]), 2))  # çakışan olaylar → 24'te kes
        if d in steps:
            r["steps"] = steps[d][0]
        if d in sleep:
            r["sleep_min"] = sleep[d][0]
        if d in reports:
            r["projects"] = sorted(json.loads(reports[d][0] or "[]"))
            r["summary"] = reports[d][1]
        rows.append(r)
    meta = {"first_day": first, "last_day": last,
            **{f"months_{g}": f"{len(m)} ({min(m, default='')}..{max(m, default='')})" for g, m in months.items()}}
    return rows, meta


def known(conn: sqlite3.Connection, day: str) -> dict:
    """t günü başında (yerel 00:00) bilinebilenler: haftanın günü + o güne önceden girilmiş takvim.

    Takvimde `created` t'den sonraysa (sonradan girilmiş) dahil edilmez; `created` yoksa da
    (iOS takvimi) bilinmiyor sayılır. Ödev notlarının 'done' bayrağı son hâli gösterir → t için
    kullanılamaz.
    """
    cutoff = conn.execute(f"SELECT strftime('%Y-%m-%dT%H:%M:%SZ', ?, '-3 hours')", (day,)).fetchone()[0]
    cal = [{"title": t, "start_utc": s, "end_utc": e, "all_day": bool(a)}
           for t, s, e, a in conn.execute("""
        SELECT title, start_utc, end_utc, coalesce(json_extract(payload,'$.all_day'),0) FROM event
        WHERE kind='calendar' AND local_date=? AND json_extract(payload,'$.created') < ?
        ORDER BY start_utc""", (day, cutoff))]
    return {"day": day, "dow": date.fromisoformat(day).isoweekday(), "calendar": cal}
