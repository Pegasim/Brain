"""iCalendar (.ics) dosyaları → 'calendar' event'leri. Tuna'nın ana takvimi Google Calendar.

Beklenen yerler (photos/Tuna doldurur):
  data/raw/google-takeout/**/Calendar/*.ics   (takeout.google.com → Takvim)
  data/raw/calendar/**/*.ics                   (vdirsyncer vb. ile yerel senkron)
Tekrarlayan olaylar (RRULE) ilk olaydan bugün+90 güne kadar açılır.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from cfv import db
from cfv.ingest import Batch
from cfv.timeutil import DEFAULT_TZ, UTC

IMPORTER = "cfv.sources.calendar_ics@1"
GLOBS = ("google-takeout/**/*.ics", "calendar/**/*.ics")
HORIZON_DAYS = 90


def find_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for g in GLOBS:
        files.extend(sorted(root.glob(g)))
    return files


def _as_dt(v) -> tuple[dt.datetime, bool]:
    """date → (yerel gece yarısı, all_day=True); naive datetime → İstanbul kabul."""
    if isinstance(v, dt.datetime):
        return (v if v.tzinfo else v.replace(tzinfo=DEFAULT_TZ)), False
    return dt.datetime.combine(v, dt.time(0), DEFAULT_TZ), True


def ingest(conn, files: list[Path] | None = None) -> dict:
    import icalendar
    import recurring_ical_events

    root = db.data_dir() / "raw"
    files = files if files is not None else find_files(root)
    if not files:
        raise FileNotFoundError(root / "google-takeout/**/Calendar/*.ics")
    stats = {"files": 0, "events": 0}
    for path in files:
        cal = icalendar.Calendar.from_ical(path.read_bytes())
        cal_name = str(cal.get("X-WR-CALNAME") or path.stem)
        batch = Batch(conn, "calendar-ics", path, IMPORTER)
        stats["files"] += 1
        starts = [c.get("DTSTART").dt for c in cal.walk("VEVENT") if c.get("DTSTART")]
        if not starts:
            continue
        lo = min(_as_dt(s)[0] for s in starts)
        hi = dt.datetime.now(UTC) + dt.timedelta(days=HORIZON_DAYS)
        seen: dict[str, int] = {}
        for ev in recurring_ical_events.of(cal).between(lo, hi):
            start, all_day = _as_dt(ev.get("DTSTART").dt)
            end = _as_dt(ev.get("DTEND").dt)[0] if ev.get("DTEND") else None
            uid = str(ev.get("UID") or f"{start.isoformat()}|{ev.get('SUMMARY')}")
            key = f"{uid}@{start.isoformat()}"
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                key += f"#{seen[key]}"
            loc = str(ev.get("LOCATION") or "") or None
            desc = str(ev.get("DESCRIPTION") or "") or None
            attendees = ev.get("ATTENDEE") or []
            if not isinstance(attendees, list):
                attendees = [attendees]
            attendees = [str(a).replace("mailto:", "") for a in attendees]
            off = int(start.utcoffset().total_seconds() // 60) if start.utcoffset() is not None else None
            batch.event(key, "calendar", start, end=end, tz_offset_min=off,
                        time_quality="day" if all_day else "exact",
                        title=str(ev.get("SUMMARY") or "") or None,
                        body="\n".join(x for x in (loc and f"Yer: {loc}", desc) if x) or None,
                        day=start.date().isoformat() if all_day else None,
                        payload={"calendar": cal_name, "account": "google", "all_day": all_day,
                                 "location": loc, "attendees": attendees or None,
                                 "status": str(ev.get("STATUS") or "") or None})
            stats["events"] += 1
    return stats
