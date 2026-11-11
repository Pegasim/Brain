"""Basit rakipler. Her biri t gününü yalnız t'den önceki satırlarla tahmin eder."""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from .targets import COUNTS, SETS


def _prev(day: str, n: int) -> str:
    return (date.fromisoformat(day) - timedelta(n)).isoformat()


def yesterday(by_day: dict[str, dict], day: str) -> dict:
    """Yarın = dün (dün NULL'sa 7 güne kadar geriye en yakın değer)."""
    out = {"day": day}
    for k in COUNTS + SETS:
        for n in range(1, 8):
            v = by_day.get(_prev(day, n), {}).get(k)
            if v is not None:
                out[k] = v
                break
    return out


def dow4(by_day: dict[str, dict], day: str) -> dict:
    """Son 4 aynı günün ortalaması (kümelerde: 4'ün en az 2'sinde görünenler). 12 haftaya kadar geriye bakar."""
    out = {"day": day}
    for k in COUNTS + SETS:
        vals = [v for w in range(1, 13) if (v := by_day.get(_prev(day, 7 * w), {}).get(k)) is not None][:4]
        if not vals:
            continue
        if k in SETS:
            c = Counter(x for v in vals for x in v)
            out[k] = sorted(x for x, m in c.items() if m >= min(2, len(vals)))
        else:
            out[k] = round(sum(vals) / len(vals), 2)
    return out


BASELINES = {"baseline_yesterday": yesterday, "baseline_dow4": dow4}


def yesterday_samples(by_day: dict[str, dict], day: str) -> dict:
    return {"day": day, "samples": [yesterday(by_day, day)]}


def dow4_samples(by_day: dict[str, dict], day: str) -> dict:
    """Son 4 aynı günün kendisi (her biri bir örnek)."""
    rows = [r for w in range(1, 13) if (r := by_day.get(_prev(day, 7 * w)))][:4]
    return {"day": day, "samples": [{k: r[k] for k in COUNTS + SETS if k in r} for r in rows]}


SAMPLERS = {"baseline_yesterday": yesterday_samples, "baseline_dow4": dow4_samples}
