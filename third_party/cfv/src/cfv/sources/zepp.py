"""Zepp / Amazfit saat (uygulama yedeği) → günlük aktivite, uyku ve egzersiz event'leri."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from cfv.ingest import Batch, drop_other_snapshots, iphone_extract, open_ro, snapshot_of
from cfv.timeutil import UTC, from_unix

IMPORTER = "cfv.sources.zepp@1"
DOCS = ("AppDomain-com.huami.watch", "Documents")  # en yeni extract-<tarih>/ altında

# sleepStage.sleepType: 4 = tüm uyku zarfı; diğerleri evreler (zarf toplamıyla doğrulandı)
SLEEP_STAGE = {1: "awake", 2: "light", 3: "rem", 5: "deep"}
# Huami spor kodları (kısmi; bilinmeyenler 'egzersiz <kod>' olarak kalır)
SPORT = {1: "koşu", 6: "yürüyüş", 8: "koşu bandı", 9: "bisiklet", 10: "kapalı bisiklet",
         12: "eliptik", 14: "havuz yüzme", 15: "açık su yüzme", 16: "serbest antrenman",
         21: "ip atlama", 23: "kürek", 60: "yoga"}


def _user_dir(docs: Path) -> Path:
    for p in sorted(docs.iterdir()):
        if p.is_dir() and p.name.isdigit() and (p / "HMCorePersistanceDatabaseV1.sqlite").exists() \
                and p.name != "0":
            return p
    raise FileNotFoundError(docs / "<kullanıcı-id>/HMCorePersistanceDatabaseV1.sqlite")


def ingest(conn, docs: Path | None = None) -> dict:
    docs = docs or iphone_extract().joinpath(*DOCS)
    udir = _user_dir(docs)
    core = open_ro(udir / "HMCorePersistanceDatabaseV1.sqlite")
    batch = Batch(conn, "zepp", udir, IMPORTER, snapshot=snapshot_of(udir))
    drop_other_snapshots(conn, "zepp", batch.source_id)
    stats = {"days": 0, "sleeps": 0, "workouts": 0}

    pai = {}
    fb_path = udir / "HMDBFirstBeatDataBaseV2.sqlite"
    if fb_path.exists():
        for r in open_ro(fb_path).execute(
                "SELECT timestamp, timeZone, dailyPAI, totalPAI, restHR, maxHR FROM device_pai_v2"):
            day = (from_unix(r["timestamp"]) + dt.timedelta(seconds=int(r["timeZone"] or 0) * 900
                   if abs(int(r["timeZone"] or 0)) < 60 else dt.timedelta(0))).date().isoformat()
            pai[day] = dict(r)

    # --- günlük aktivite ---
    for r in core.execute("""SELECT dateStr, dayTimestamp, timeZone, stepCount, stepDistance,
                                    stepCalories, walkTime, runTime, restHeartRate, todayMaxHR, stepGoal
                             FROM activitySummary WHERE dateStr IS NOT NULL ORDER BY dateStr"""):
        off = int(r["timeZone"]) // 60 if r["timeZone"] else 180
        start = from_unix(r["dayTimestamp"])
        p = pai.get(r["dateStr"], {})
        steps = r["stepCount"] or 0
        payload = {"steps": steps, "distance_m": r["stepDistance"], "calories": r["stepCalories"],
                   "walk_min": r["walkTime"], "run_min": r["runTime"], "step_goal": r["stepGoal"],
                   "rest_hr": r["restHeartRate"] or p.get("restHR") or None,
                   "max_hr": r["todayMaxHR"] or p.get("maxHR") or None,
                   "pai_daily": round(p["dailyPAI"], 1) if p.get("dailyPAI") else None,
                   "pai_total": p.get("totalPAI")}
        if not steps and not payload["pai_daily"]:
            continue
        batch.event(f"day/{r['dateStr']}", "zepp_day", start, end=start + dt.timedelta(days=1),
                    tz_offset_min=off, time_quality="day", day=r["dateStr"],
                    title=f"{steps:,} adım".replace(",", "."), payload=payload)
        stats["days"] += 1

    # --- uyku (gece başına zarf + evre toplamları; dakikalar gün başına göre, negatif = önceki akşam)
    nights: dict[int, dict] = {}
    for r in core.execute("SELECT currentDate, sleepType, startTime, endTime FROM sleepStage"):
        n = nights.setdefault(r["currentDate"], {"env": None, "stages": {}, "span": None})
        sp = n["span"]
        n["span"] = (r["startTime"], r["endTime"]) if sp is None else (
            min(sp[0], r["startTime"]), max(sp[1], r["endTime"]))
        if r["sleepType"] == 4:
            s, e = r["startTime"], r["endTime"]
            env = n["env"]
            n["env"] = (s, e) if env is None else (min(env[0], s), max(env[1], e))
        else:
            k = SLEEP_STAGE.get(r["sleepType"], f"t{r['sleepType']}")
            n["stages"][k] = n["stages"].get(k, 0) + (r["endTime"] - r["startTime"] + 1)
    for day_ts, n in sorted(nights.items()):
        env = n["env"] or n["span"]  # zarf yoksa evrelerin kapsadığı aralık
        base = from_unix(day_ts)
        start = base + dt.timedelta(minutes=env[0])
        end = base + dt.timedelta(minutes=env[1] + 1)
        total = int((end - start).total_seconds() // 60)
        if total < 20:
            continue
        local_day = (base + dt.timedelta(hours=3)).date().isoformat()
        asleep = sum(n["stages"].get(k, 0) for k in ("light", "deep", "rem"))
        h, m = divmod(asleep or total, 60)  # başlık: uyunan süre (uyanıklık hariç)
        batch.event(f"sleep/{day_ts}", "sleep", start, end=end, tz_offset_min=180, day=local_day,
                    title=f"uyku {h}s {m:02d}dk",
                    payload={"total_min": total, "asleep_min": asleep or None, "envelope": n["env"] is not None,
                             **{f"{k}_min": v for k, v in n["stages"].items()}})
        stats["sleeps"] += 1

    # --- egzersizler ---
    hm_path = udir / "healthMatrix.sqlite"
    if hm_path.exists():
        hm = open_ro(hm_path)
        for r in hm.execute("""SELECT w.dataId, w.activityType, w.duration, s.startDate, s.endDate,
                                      d.timeZone
                               FROM Workout w JOIN Samples s ON s.dataId = w.dataId
                               LEFT JOIN DataSource d ON d.sourceId = s.dataSourceId"""):
            start, end = from_unix(r["startDate"], ms=True), from_unix(r["endDate"], ms=True)
            from cfv.timeutil import offset_of
            off = offset_of(start, r["timeZone"])
            name = SPORT.get(r["activityType"], f"egzersiz {r['activityType']}")
            mins = round((r["duration"] or (end - start).total_seconds()) / 60)
            pts = hm.execute("SELECT count(*) FROM WorkoutLocation WHERE activityId=?",
                             (r["dataId"],)).fetchone()[0]
            batch.event(f"workout/{r['dataId']}", "workout", start, end=end, tz_offset_min=off,
                        title=f"{name} {mins} dk",
                        payload={"activity_type": r["activityType"], "duration_min": mins,
                                 "tz": r["timeZone"], "gps_points": pts or None})
            stats["workouts"] += 1
    return stats
