"""Apple Sağlık (healthdb_secure.sqlite + healthdb.sqlite) → ölçüm, günlük aktivite, uyku ve egzersiz event'leri.

Yalnız ŞİFRELİ yedekte bulunur (HealthDomain/Health/). Yalnız cfv-spor'un istedikleri ve uyku okunur:
  measurement  tartı: kilo, yağ %, yağsız kütle, BMI (Mi Home → Sağlık; kas kütlesi Sağlık'a gelmez)
  health_day   günlük: adım, aktif (egzersiz) dakika, dinlenik/min/ort/maks nabız — anahtarlar zepp_day ile aynı
  sleep        gece başına evre toplamları — anahtarlar zepp.py'nin sleep olayıyla aynı (light = Sağlık'ın 'core')
  workout      tür, süre, ortalama/maks nabız

Şema: samples(data_id, start_date, end_date, data_type) + quantity_samples(data_id, quantity, original_quantity,
original_unit → unit_strings) / category_samples(data_id, value); objects(data_id, provenance) →
data_provenances(source_id, tz_name) → healthdb.sqlite sources(bundle_id, name). Egzersiz: workouts +
(iOS 16+) workout_activities. Zamanlar Core Data saniyesi.

DOĞRULAMA: aşağıdaki data_type kodları ve birim çevrimi sentetik testle sınanır ama gerçek şifreli yedek henüz yok.
Fiziksel aralık dışındaki değerler atılır ve stats['out_of_range']'te türe göre sayılır; stats['ranges'] her türün
min/orta/maks değerini verir. İlk gerçek çalıştırmada bu iki alan elle kontrol edilecek: kod yanlışsa sessiz çöp
değil, büyük out_of_range sayısı görülür.

ZEPP ÇAKIŞMASI (provenance): Zepp uygulaması da Sağlık'a yazar (uyku evreleri, nabız, adım, egzersiz). zepp.py'nin
(uygulama yedeği) kapsadığı günlerde:
  - o günün health_day olayı hiç üretilmez (zepp_day zaten var; iPhone adımıyla iki kez sayılmasın),
  - o gecenin sleep olayı üretilmez (kaynağı ne olursa olsun: aynı gece için tek uyku olayı),
  - Zepp kaynaklı egzersiz, zepp.py'de ±10 dk içinde başlayan bir egzersiz varsa üretilmez.
Bu yüzden SOURCES'ta zepp'ten SONRA koşar. Kapsanmayan günlerde Zepp kaynağı tercih edilir (saat > telefon).
"""

from __future__ import annotations

import bisect
import datetime as dt
import statistics
from pathlib import Path

from cfv.ingest import Batch, drop_other_snapshots, iphone_extract, open_ro, snapshot_of
from cfv.timeutil import from_coredata, iso, local_date, offset_of

IMPORTER = "cfv.sources.health@1"
HEALTH = ("HealthDomain", "Health")  # en yeni extract-<tarih>/ altında
SECURE, MAIN = "healthdb_secure.sqlite", "healthdb.sqlite"

# HealthKit data_type kodları (healthdb_secure.samples.data_type)
BMI, BODY_FAT, BODY_MASS, LEAN_MASS, HEART_RATE, STEPS = 0, 1, 3, 4, 5, 7
SLEEP, EXERCISE_TIME, WORKOUT, RESTING_HR = 63, 75, 79, 118
QUANTITY_TYPES = (BMI, BODY_FAT, BODY_MASS, LEAN_MASS, HEART_RATE, STEPS, EXERCISE_TIME, RESTING_HR)
TYPE_NAME = {BMI: "bmi", BODY_FAT: "fat_pct", BODY_MASS: "weight_kg", LEAN_MASS: "lean_kg",
             HEART_RATE: "hr", STEPS: "steps", EXERCISE_TIME: "active_min", RESTING_HR: "rest_hr"}
# fiziksel aralık (çevrilmiş birimle); adım ve aktif dakika ÖRNEK başına
RANGE = {BMI: (10, 70), BODY_FAT: (2, 70), BODY_MASS: (20, 300), LEAN_MASS: (15, 200),
         HEART_RATE: (25, 250), RESTING_HR: (25, 150), STEPS: (0, 100_000), EXERCISE_TIME: (0, 1440)}
# HKCategoryValueSleepAnalysis
SLEEP_VALUE = {0: "in_bed", 1: "asleep", 2: "awake", 3: "light", 4: "deep", 5: "rem"}
ASLEEP = ("asleep", "light", "deep", "rem")
# HKWorkoutActivityType (kısmi; bilinmeyen 'egzersiz <kod>')
ACTIVITY = {13: "bisiklet", 16: "eliptik", 20: "fonksiyonel kuvvet", 24: "doğa yürüyüşü", 35: "kürek",
            37: "koşu", 46: "yüzme", 50: "ağırlık antrenmanı", 52: "yürüyüş", 57: "yoga", 59: "core",
            63: "HIIT", 73: "karışık kardiyo", 3000: "diğer"}

ZEPP_BUNDLES = {"com.huami.watch", "com.huami.midong", "com.xiaomi.mifit"}
LABEL = {"com.xiaomi.mihome": "mihome", **{b: "zepp" for b in ZEPP_BUNDLES}}
NIGHT_GAP = dt.timedelta(hours=4)
ZEPP_WORKOUT_MATCH = dt.timedelta(minutes=10)


def label_of(bundle: str | None) -> str:
    if not bundle:
        return "bilinmiyor"
    if bundle in LABEL:
        return LABEL[bundle]
    if bundle.startswith("com.apple.health"):
        return "iphone"  # cihazın kendi kaynağı (com.apple.health.<UUID>) ve elle giriş
    return bundle


def convert(dtype: int, q: float | None, oq: float | None, unit: str | None) -> tuple[float | None, bool]:
    """(değer, yedek_yol_mu). Önce özgün birim (unit_strings), yoksa kanonik quantity + sezgi."""
    u = (unit or "").strip()
    if oq is not None and u:
        if dtype in (BODY_MASS, LEAN_MASS):
            f = {"kg": 1, "g": 0.001, "lb": 0.45359237, "st": 6.35029318}.get(u)
            if f:
                return oq * f, False
        elif dtype == BODY_FAT and u == "%":
            return (oq * 100 if oq <= 1 else oq), False
        elif dtype in (HEART_RATE, RESTING_HR):
            f = {"count/min": 1, "count/s": 60}.get(u)
            if f:
                return oq * f, False
        elif dtype == EXERCISE_TIME:
            f = {"min": 1, "s": 1 / 60, "hr": 60}.get(u)
            if f:
                return oq * f, False
        elif dtype in (STEPS, BMI) and u == "count":
            return oq, False
    if q is None:
        return None, True
    if dtype in (BODY_MASS, LEAN_MASS):
        return (q / 1000 if q > 1000 else q), True
    if dtype == BODY_FAT:
        return (q * 100 if q <= 1 else q), True
    if dtype in (HEART_RATE, RESTING_HR):
        return (q * 60 if q < 5 else q), True
    if dtype == EXERCISE_TIME:
        return (q / 60 if q > 5 else q), True
    return q, True


def _cols(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _hm(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h}s {m:02d}dk"


def zepp_coverage(conn) -> tuple[set[str], set[str], list[dt.datetime]]:
    """zepp.py'nin (uygulama yedeği) kapsadığı günler, geceler ve egzersiz başlangıçları."""
    rows = conn.execute("""SELECT e.kind, e.local_date, e.start_utc FROM event e JOIN source s ON s.id = e.source_id
                           WHERE s.kind = 'zepp' AND e.kind IN ('zepp_day', 'sleep', 'workout')""").fetchall()
    days = {d for k, d, _ in rows if k == "zepp_day"}
    nights = {d for k, d, _ in rows if k == "sleep"}
    workouts = sorted(dt.datetime.fromisoformat(s.replace("Z", "+00:00")) for k, _, s in rows if k == "workout")
    return days, nights, workouts


def ingest(conn, health: Path | None = None) -> dict:
    health = health or iphone_extract().joinpath(*HEALTH)
    sec = open_ro(health / SECURE)
    batch = Batch(conn, "health", health / SECURE, IMPORTER, snapshot=snapshot_of(health))
    drop_other_snapshots(conn, "health", batch.source_id)
    stats = {"measurements": 0, "days": 0, "sleeps": 0, "workouts": 0, "days_covered_by_zepp": 0,
             "nights_covered_by_zepp": 0, "workouts_covered_by_zepp": 0, "unit_fallbacks": 0,
             "out_of_range": {}, "ranges": {}}

    bundles: dict[int, tuple[str | None, str | None]] = {}
    if (health / MAIN).exists():
        main = open_ro(health / MAIN)
        if "bundle_id" in _cols(main, "sources"):
            bundles = {r["ROWID"]: (r["bundle_id"], r["name"])
                       for r in main.execute("SELECT ROWID, bundle_id, name FROM sources")}
    prov_cols = _cols(sec, "data_provenances")
    prov = {r["ROWID"]: (bundles.get(r["source_id"], (None, None))[0], r["tz"]) for r in sec.execute(
        f"SELECT ROWID, source_id, {'tz_name' if 'tz_name' in prov_cols else 'NULL'} tz FROM data_provenances")}
    units = ({r["ROWID"]: r["unit_string"] for r in sec.execute("SELECT ROWID, unit_string FROM unit_strings")}
             if _cols(sec, "unit_strings") else {})
    qcols = _cols(sec, "quantity_samples")
    oq = "q.original_quantity" if "original_quantity" in qcols else "NULL"
    ou = "q.original_unit" if "original_unit" in qcols else "NULL"

    zdays, znights, zworkouts = zepp_coverage(conn)

    def place(t: dt.datetime, prov_id) -> tuple[str, int | None, str]:
        bundle, tz = prov.get(prov_id, (None, None))
        off = offset_of(t, tz)
        return label_of(bundle), off, local_date(t, off)

    # --- nicel örnekler ---
    values: dict[int, list[float]] = {}
    weigh: dict[tuple[str, str], dict] = {}                 # (kaynak, dakika) → ölçüm
    daily: dict[str, dict[str, dict]] = {}                  # gün → kaynak → toplamlar
    hr: list[tuple[dt.datetime, float, str]] = []
    for r in sec.execute(f"""SELECT s.data_id, s.start_date, s.end_date, s.data_type, q.quantity, {oq} oq,
                                    {ou} ou, o.provenance
                             FROM samples s JOIN quantity_samples q ON q.data_id = s.data_id
                             JOIN objects o ON o.data_id = s.data_id
                             WHERE s.data_type IN ({','.join(map(str, QUANTITY_TYPES))})"""):
        dtype = r["data_type"]
        v, fallback = convert(dtype, r["quantity"], r["oq"], units.get(r["ou"]))
        t = from_coredata(r["start_date"])
        if v is None or t is None:
            continue
        stats["unit_fallbacks"] += fallback
        lo, hi = RANGE[dtype]
        if not lo <= v <= hi:
            name = TYPE_NAME[dtype]
            stats["out_of_range"][name] = stats["out_of_range"].get(name, 0) + 1
            continue
        values.setdefault(dtype, []).append(v)
        src, off, day = place(t, r["provenance"])
        if dtype in (BMI, BODY_FAT, BODY_MASS, LEAN_MASS):
            m = weigh.setdefault((src, iso(t)[:16]), {"t": t, "off": off, "source": src})
            m[TYPE_NAME[dtype]] = round(v, 2)
            continue
        d = daily.setdefault(day, {}).setdefault(src, {"steps": 0.0, "active_min": 0.0, "hr": [], "rest": []})
        if dtype == STEPS:
            d["steps"] += v
        elif dtype == EXERCISE_TIME:
            d["active_min"] += v
        elif dtype == HEART_RATE:
            d["hr"].append(v)
            hr.append((t, v, src))
        elif dtype == RESTING_HR:
            d["rest"].append((t, v))
    stats["ranges"] = {TYPE_NAME[k]: [round(min(v), 2), round(statistics.median(v), 2), round(max(v), 2)]
                       for k, v in sorted(values.items())}

    # --- tartı ---
    for (src, minute), m in sorted(weigh.items(), key=lambda kv: kv[1]["t"]):
        if "weight_kg" not in m and "fat_pct" not in m:
            continue  # yalnız BMI/yağsız kütle: tek başına ölçüm sayılmaz
        payload = {k: m.get(k) for k in ("weight_kg", "fat_pct", "lean_kg", "bmi")}
        title = f"{m['weight_kg']:.1f} kg".replace(".", ",") if m.get("weight_kg") else f"yağ %{m['fat_pct']:.1f}"
        batch.event(f"measurement/{src}/{minute}", "measurement", m["t"], tz_offset_min=m["off"], title=title,
                    payload={**payload, "source": src})
        stats["measurements"] += 1

    # --- günlük ---
    for day, by_src in sorted(daily.items()):
        if day in zdays:
            stats["days_covered_by_zepp"] += 1
            continue
        pick = by_src.get("zepp") or max(by_src.values(), key=lambda d: (d["steps"], len(d["hr"])))
        src = next(k for k, v in by_src.items() if v is pick)
        hrs = pick["hr"] or [x for d in by_src.values() for x in d["hr"]]
        rest = pick["rest"] or [x for d in by_src.values() for x in d["rest"]]
        steps = round(pick["steps"])
        payload = {"steps": steps or None,
                   "active_min": round(sum(d["active_min"] for d in by_src.values())) or None,
                   "rest_hr": round(max(rest)[1]) if rest else None,
                   "min_hr": round(min(hrs)) if hrs else None,
                   "avg_hr": round(statistics.fmean(hrs)) if hrs else None,
                   "max_hr": round(max(hrs)) if hrs else None,
                   "source": src,
                   "steps_by_source": {k: round(v["steps"]) for k, v in by_src.items() if v["steps"]} or None}
        if not any(payload[k] for k in ("steps", "active_min", "rest_hr", "max_hr")):
            continue
        start = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(), dt.timezone(dt.timedelta(hours=3)))
        batch.event(f"day/{day}", "health_day", start, end=start + dt.timedelta(days=1), tz_offset_min=180,
                    time_quality="day", day=day,
                    title=f"{steps:,} adım".replace(",", ".") if steps else "sağlık günü", payload=payload)
        stats["days"] += 1

    # --- uyku: kaynak başına seanslar (4 saatten uzun boşluk yeni seans), gece = bitişin yerel günü ---
    by_src: dict[str, list] = {}
    for r in sec.execute("""SELECT DISTINCT s.start_date, s.end_date, c.value, o.provenance
                            FROM samples s JOIN category_samples c ON c.data_id = s.data_id
                            JOIN objects o ON o.data_id = s.data_id
                            WHERE s.data_type = ? ORDER BY s.start_date""", (SLEEP,)):
        a, b = from_coredata(r["start_date"]), from_coredata(r["end_date"])
        if a is None or b is None or b <= a or r["value"] not in SLEEP_VALUE:
            continue
        bundle, tz = prov.get(r["provenance"], (None, None))
        by_src.setdefault(label_of(bundle), []).append((a, b, SLEEP_VALUE[r["value"]], tz))
    nights: dict[str, list[dict]] = {}
    for src, rows in by_src.items():
        sessions: list[list] = []
        for row in rows:
            if sessions and row[0] - max(x[1] for x in sessions[-1]) <= NIGHT_GAP:
                sessions[-1].append(row)
            else:
                sessions.append([row])
        for ss in sessions:
            mins: dict[str, int] = {}
            for a, b, v, _ in ss:
                mins[v] = mins.get(v, 0) + round((b - a).total_seconds() / 60)
            core = [x for x in ss if x[2] in ASLEEP] or ss
            start, end = min(x[0] for x in core), max(x[1] for x in core)
            off = offset_of(end, ss[-1][3])
            nights.setdefault(local_date(end, off), []).append(
                {"src": src, "start": start, "end": end, "off": off, "mins": mins})
    for day, cands in sorted(nights.items()):
        if day in znights:
            stats["nights_covered_by_zepp"] += 1
            continue
        staged = lambda c: any(k in c["mins"] for k in ("light", "deep", "rem"))  # noqa: E731
        n = max(cands, key=lambda c: (c["src"] == "zepp", staged(c), c["end"] - c["start"]))
        total = round((n["end"] - n["start"]).total_seconds() / 60)
        if total < 20:
            continue
        asleep = sum(n["mins"].get(k, 0) for k in ASLEEP)
        batch.event(f"sleep/{day}", "sleep", n["start"], end=n["end"], tz_offset_min=n["off"], day=day,
                    title=f"uyku {_hm(asleep or total)}",
                    payload={"total_min": total, "asleep_min": asleep or None, "source": n["src"],
                             **{f"{k}_min": v for k, v in sorted(n["mins"].items())}})
        stats["sleeps"] += 1

    # --- egzersizler ---
    hr.sort(key=lambda x: x[0])
    hr_t = [x[0] for x in hr]
    tables = {r[0] for r in sec.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "workout_activities" in tables:
        q = """SELECT s.data_id, s.start_date, s.end_date, o.provenance, a.activity_type, a.duration
               FROM samples s JOIN objects o ON o.data_id = s.data_id
               LEFT JOIN workout_activities a ON a.owner_id = s.data_id AND a.is_primary_activity = 1
               WHERE s.data_type = ?"""
    else:
        q = """SELECT s.data_id, s.start_date, s.end_date, o.provenance, w.activity_type, w.duration
               FROM samples s JOIN objects o ON o.data_id = s.data_id JOIN workouts w ON w.data_id = s.data_id
               WHERE s.data_type = ?"""
    for r in sec.execute(q, (WORKOUT,)):
        start, end = from_coredata(r["start_date"]), from_coredata(r["end_date"])
        if start is None or end is None:
            continue
        src, off, _ = place(start, r["provenance"])
        if src == "zepp":
            i = bisect.bisect_left(zworkouts, start - ZEPP_WORKOUT_MATCH)
            if i < len(zworkouts) and zworkouts[i] <= start + ZEPP_WORKOUT_MATCH:
                stats["workouts_covered_by_zepp"] += 1
                continue
        lo, hi = bisect.bisect_left(hr_t, start), bisect.bisect_right(hr_t, end)
        window = [x for x in hr[lo:hi] if x[2] == src] or hr[lo:hi]
        bpm = [x[1] for x in window]
        mins = round((r["duration"] or (end - start).total_seconds()) / 60)
        name = ACTIVITY.get(r["activity_type"], f"egzersiz {r['activity_type']}")
        batch.event(f"workout/{r['data_id']}", "workout", start, end=end, tz_offset_min=off,
                    title=f"{name} {mins} dk",
                    payload={"activity_type": r["activity_type"], "activity": name, "duration_min": mins,
                             "avg_hr": round(statistics.fmean(bpm)) if bpm else None,
                             "max_hr": round(max(bpm)) if bpm else None, "source": src})
        stats["workouts"] += 1
    return stats
