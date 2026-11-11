"""iPhone Calendar.sqlitedb → 'calendar' event'leri.

Tuna'nın asıl takvimi Google (bkz. calendar_ics); telefonda iCloud takvimleri,
"Found in Mail" ve zamanlanmış anımsatıcılar var. Doğum günleri ve resmî tatiller atlanır.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from cfv.ingest import Batch, drop_other_snapshots, iphone_extract, open_ro, snapshot_of
from cfv.timeutil import DEFAULT_TZ, from_coredata, offset_of

IMPORTER = "cfv.sources.calendar_ios@1"
DEFAULT_DB = ("HomeDomain", "Library", "Calendar", "Calendar.sqlitedb")  # en yeni extract-<tarih>/ altında
SKIP_CALENDARS = {"Birthdays", "Doğum Günleri", "Tatiller (Türkiye)"}


def ingest(conn, db_path: Path | None = None) -> dict:
    db_path = db_path or iphone_extract().joinpath(*DEFAULT_DB)
    src = open_ro(db_path)
    batch = Batch(conn, "calendar-ios", db_path, IMPORTER, snapshot=snapshot_of(db_path))
    drop_other_snapshots(conn, "calendar-ios", batch.source_id)
    stats = {"events": 0, "skipped_calendars": 0, "occurrences": 0}

    occ: dict[int, list[tuple[float, float | None]]] = {}
    for r in src.execute("SELECT event_id, occurrence_start_date, occurrence_end_date FROM OccurrenceCache"):
        occ.setdefault(r["event_id"], []).append((r["occurrence_start_date"], r["occurrence_end_date"]))

    rows = src.execute("""
        SELECT ci.ROWID, ci.UUID, ci.summary, ci.description, ci.start_date, ci.end_date, ci.start_tz,
               ci.all_day, ci.has_recurrences, ci.url, cal.title cal, s.name store,
               l.title loc, l.address addr, l.latitude lat, l.longitude lon
        FROM CalendarItem ci
        JOIN Calendar cal ON cal.ROWID = ci.calendar_id
        JOIN Store s ON s.ROWID = cal.store_id
        LEFT JOIN Location l ON l.ROWID = ci.location_id""").fetchall()

    for r in rows:
        if r["cal"] in SKIP_CALENDARS:
            stats["skipped_calendars"] += 1
            continue
        attendees = [p["email"] or p["phone_number"] for p in src.execute(
            "SELECT email, phone_number FROM Participant WHERE owner_id=? AND is_self=0", (r["ROWID"],))]
        attendees = [a.replace("mailto:", "") for a in attendees if a]
        times = occ.get(r["ROWID"]) if r["has_recurrences"] else None
        times = times or [(r["start_date"], r["end_date"])]
        for i, (s0, e0) in enumerate(times):
            start, end = from_coredata(s0), from_coredata(e0)
            if start is None:
                continue
            all_day = bool(r["all_day"])
            tz = None if (r["start_tz"] in (None, "_float")) else r["start_tz"]
            off = offset_of(start, tz) if tz else offset_of(start, "Europe/Istanbul")
            if all_day:  # tüm gün olayları "yüzen" saattir: UTC değeri yerel gece yarısı
                day = start.date().isoformat()
                start = dt.datetime.combine(start.date(), dt.time(0), DEFAULT_TZ)
                end = start + dt.timedelta(days=max(1, ((from_coredata(e0) or start) - from_coredata(s0)).days))
            else:
                day = None
            loc = " · ".join(x for x in (r["loc"], r["addr"]) if x) or None
            body = "\n".join(x for x in (loc and f"Yer: {loc}", r["description"]) if x) or None
            batch.event(f"{r['UUID']}#{i}" if len(times) > 1 else r["UUID"], "calendar", start,
                        end=end, tz_offset_min=off, time_quality="day" if all_day else "exact",
                        title=r["summary"], body=body, lat=r["lat"] or None, lon=r["lon"] or None,
                        day=day,
                        payload={"calendar": r["cal"], "account": r["store"], "all_day": all_day,
                                 "location": loc, "attendees": attendees or None, "url": r["url"]})
            stats["events"] += 1
            stats["occurrences"] += len(times) > 1
    return stats
