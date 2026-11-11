"""Kaynaklar arası basit eşleştirmeler → event_link. Her çağrıda baştan üretilir.

photo_at: foto/video, saatli bir takvim olayının süresi içinde (±30 dk) çekildiyse o olaya;
çok günlü tüm-gün olaylarında (2-14 gün: gezi, kamp) o günlerde çekildiyse o olaya bağlanır.
Tek günlük tüm-gün olayları ("vize", "randevu") bağlanmaz: o günün bütün fotoları gürültü olur.
"""

from __future__ import annotations

MARGIN = "30 minutes"
MAX_TIMED_HOURS = 12
ALLDAY_DAYS = (2, 14)


def calendar_photos(conn) -> dict:
    conn.execute("DELETE FROM event_link WHERE kind = 'photo_at'")
    timed = conn.execute(f"""
        INSERT OR IGNORE INTO event_link (a, b, kind)
        SELECT p.id, c.id, 'photo_at'
        FROM event c JOIN event p
          ON p.kind IN ('photo', 'video')
         AND p.start_utc >= strftime('%Y-%m-%dT%H:%M:%SZ', c.start_utc, '-{MARGIN}')
         AND p.start_utc <= strftime('%Y-%m-%dT%H:%M:%SZ', c.end_utc, '+{MARGIN}')
        WHERE c.kind = 'calendar' AND c.time_quality = 'exact' AND c.end_utc IS NOT NULL
          AND (julianday(c.end_utc) - julianday(c.start_utc)) * 24 <= {MAX_TIMED_HOURS}""").rowcount
    allday = conn.execute(f"""
        INSERT OR IGNORE INTO event_link (a, b, kind)
        SELECT p.id, c.id, 'photo_at'
        FROM event c JOIN event p
          ON p.kind IN ('photo', 'video')
         AND p.local_date >= c.local_date
         AND p.local_date < date(c.local_date, '+' || CAST(round(julianday(c.end_utc) - julianday(c.start_utc)) AS INT) || ' days')
        WHERE c.kind = 'calendar' AND c.time_quality = 'day'
          AND julianday(c.end_utc) - julianday(c.start_utc) BETWEEN {ALLDAY_DAYS[0] - 0.01} AND {ALLDAY_DAYS[1] + 0.01}""").rowcount
    events = conn.execute("SELECT count(DISTINCT b) FROM event_link WHERE kind='photo_at'").fetchone()[0]
    return {"timed_links": timed, "allday_links": allday, "calendar_events_with_photos": events}


def run_all(conn) -> dict:
    with conn:
        return {"calendar_photos": calendar_photos(conn)}
