"""Zaman dönüşümleri. Tüm event zamanları UTC ISO-8601 ('...Z') saklanır."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

COREDATA_EPOCH = 978307200  # 2001-01-01T00:00:00Z
UTC = dt.timezone.utc
DEFAULT_TZ = ZoneInfo("Europe/Istanbul")

TR_MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos",
             "Eylül", "Ekim", "Kasım", "Aralık"]
TR_DAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]


def from_coredata(ts: float | None) -> dt.datetime | None:
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(ts + COREDATA_EPOCH, UTC)


def from_unix(ts: float | None, ms: bool = False) -> dt.datetime | None:
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(ts / 1000 if ms else ts, UTC)


def iso(t: dt.datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_date(t: dt.datetime, offset_min: int | None = None) -> str:
    """Olayın yaşandığı yerel gün (klasörleme bununla yapılır)."""
    if offset_min is not None:
        tz = dt.timezone(dt.timedelta(minutes=offset_min))
        return t.astimezone(tz).date().isoformat()
    return t.astimezone(DEFAULT_TZ).date().isoformat()


def offset_of(t: dt.datetime, tz: str | None) -> int | None:
    """IANA adı ('Europe/Istanbul') veya 'GMT+0300' → dakika."""
    if not tz:
        return None
    try:
        off = t.astimezone(ZoneInfo(tz)).utcoffset()
        return int(off.total_seconds() // 60)
    except Exception:
        pass
    s = tz.replace("GMT", "").replace("UTC", "")
    if s and s[0] in "+-" and s[1:].isdigit():
        sign = -1 if s[0] == "-" else 1
        hh, mm = int(s[1:3]), int(s[3:5] or 0)
        return sign * (hh * 60 + mm)
    return None


def iso_week_monday(year: int, week: int) -> dt.date:
    return dt.date.fromisocalendar(year, week, 1)
