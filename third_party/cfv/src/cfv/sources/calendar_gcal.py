"""Google Takvim (gog API anlık görüntüsü) → 'calendar' event'leri.

Ham veri: data/raw/google-calendar/events.json (bkz. cfv.gcal_sync). Tekrarlayan olaylar
API tarafından açılmış gelir (her tekrar ayrı olay, `recurringEventId` ortak).
Resmî tatil takvimi atlanır.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from cfv import db
from cfv.ingest import Batch
from cfv.timeutil import DEFAULT_TZ

IMPORTER = "cfv.sources.calendar_gcal@1"
SKIP_CALENDAR_SUFFIX = ("#holiday@group.v.calendar.google.com",)


def default_path() -> Path:
    return db.data_dir() / "raw" / "google-calendar" / "events.json"


def _when(v: dict) -> tuple[dt.datetime, bool]:
    if v.get("dateTime"):
        return dt.datetime.fromisoformat(v["dateTime"]), False
    return dt.datetime.combine(dt.date.fromisoformat(v["date"]), dt.time(0), DEFAULT_TZ), True


def ingest(conn, path: Path | None = None) -> dict:
    path = path or default_path()
    if not path.exists():
        raise FileNotFoundError(path)
    snap = json.loads(path.read_text())
    names = {c["id"]: c.get("summary") or c["id"] for c in snap.get("calendars", [])}
    batch = Batch(conn, "calendar-google", path, IMPORTER, snapshot=snap.get("fetched_at"))
    stats = {"events": 0, "skipped": 0}
    for e in snap["events"]:
        cid = e["CalendarID"]
        if cid.endswith(SKIP_CALENDAR_SUFFIX) or e.get("status") == "cancelled" or not e.get("start"):
            stats["skipped"] += 1
            continue
        start, all_day = _when(e["start"])
        end = _when(e["end"])[0] if e.get("end") else None
        off = int(start.utcoffset().total_seconds() // 60)
        loc = e.get("location") or None
        attendees = [a["email"] for a in e.get("attendees") or [] if not a.get("self") and a.get("email")]
        batch.event(f"{cid}/{e['id']}", "calendar", start, end=end, tz_offset_min=off,
                    time_quality="day" if all_day else "exact",
                    title=e.get("summary") or None,
                    body="\n".join(x for x in (loc and f"Yer: {loc}", e.get("description")) if x) or None,
                    day=start.date().isoformat() if all_day else None,
                    payload={"calendar": names.get(cid, cid), "account": "google", "all_day": all_day,
                             "location": loc, "attendees": attendees or None,
                             "event_type": e.get("eventType"), "recurring": e.get("recurringEventId"),
                             "url": e.get("htmlLink"), "created": e.get("created"),
                             "updated": e.get("updated")})
        stats["events"] += 1
    return stats
