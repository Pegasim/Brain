"""Gün içi saatlik durum dizisi (yerel saat = UTC+3).

Her saat dilimi bir durum: mesaj (0 yok, 1 yalnız gelen, 2 benden) × foto (0/1) × takvim (0/1)
→ 12 durum. Her dilimin ayrıca bir emisyon vektörü var (o saatte başlayan sayılar), gün
toplamları bu vektörlerin toplamıdır.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

N_STATES = 12
EMIT = ["msgs_sent", "msgs_recv", "sessions", "sessions_me", "photos", "screenshots",
        "cal_events", "cal_hours"]
TZ_H = 3


def state(msg: int, photo: bool, cal: bool) -> int:
    return msg + 3 * int(photo) + 6 * int(cal)


def utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _local_hour(iso: str) -> tuple[str, int]:
    t = utc(iso) + timedelta(hours=TZ_H)
    return t.date().isoformat(), t.hour


def load(cfv: sqlite3.Connection, common: sqlite3.Connection, first: str, last: str) -> dict:
    """first..last günleri için {"days": [...], "states": [24*gün], "emit": [24*gün][len(EMIT)]}."""
    d0 = date.fromisoformat(first)
    days = [(d0 + timedelta(n)).isoformat() for n in range((date.fromisoformat(last) - d0).days + 1)]
    idx = {d: i for i, d in enumerate(days)}
    emit = [[0.0] * len(EMIT) for _ in range(24 * len(days))]
    cal_on = [False] * len(emit)

    def slot(day: str, hour: int) -> int | None:
        i = idx.get(day)
        return None if i is None else 24 * i + hour

    def add(key: tuple[str, int], col: int, v: float) -> None:
        if (s := slot(*key)) is not None:
            emit[s][col] += v

    for ts, me in cfv.execute(
            "SELECT start_utc, coalesce(json_extract(payload,'$.from_me'),0) FROM event "
            "WHERE kind='whatsapp' AND coalesce(json_extract(payload,'$.type'),'') != 'system'"):
        add(_local_hour(ts), 0 if me else 1, 1)
    for ts, n_me in common.execute("SELECT start_utc, n_me FROM session"):
        k = _local_hour(ts)
        add(k, 2, 1)
        add(k, 3, int(n_me > 0))
    for ts, wa, ss in cfv.execute("""
            SELECT start_utc,
              EXISTS (SELECT 1 FROM tag t WHERE t.event_id=e.id AND t.tag='album:WhatsApp'),
              EXISTS (SELECT 1 FROM tag t WHERE t.event_id=e.id AND t.tag='screenshot')
            FROM event e WHERE kind IN ('photo','video')"""):
        if ss:
            add(_local_hour(ts), 5, 1)
        elif not wa:
            add(_local_hour(ts), 4, 1)
    for a, b in cfv.execute(
            "SELECT start_utc, end_utc FROM event WHERE kind='calendar' AND end_utc IS NOT NULL "
            "AND time_quality != 'day' AND coalesce(json_extract(payload,'$.all_day'),0)=0"):
        ta, tb = utc(a) + timedelta(hours=TZ_H), utc(b) + timedelta(hours=TZ_H)
        hours = min(24.0, max(0.0, (tb - ta).total_seconds() / 3600))
        add((ta.date().isoformat(), ta.hour), 6, 1)
        add((ta.date().isoformat(), ta.hour), 7, hours)
        t = ta.replace(minute=0, second=0, microsecond=0)
        while t < tb and t < ta + timedelta(hours=24):
            if (s := slot(t.date().isoformat(), t.hour)) is not None:
                cal_on[s] = True
            t += timedelta(hours=1)

    states = []
    for e, c in zip(emit, cal_on):
        msg = 2 if e[0] else (1 if e[1] else 0)
        states.append(state(msg, e[4] + e[5] > 0, c))
    return {"days": days, "states": states, "emit": emit}
