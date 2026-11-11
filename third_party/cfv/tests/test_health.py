"""Apple Sağlık okuyucusu testleri — tamamen SENTETİK veri, gerçek ölçüm/kişi yok. Repo PUBLIC."""

import datetime as dt
import json
import sqlite3

import pytest

from cfv import db
from cfv.ingest import Batch
from cfv.sources import health
from cfv.timeutil import COREDATA_EPOCH

TR = dt.timezone(dt.timedelta(hours=3))
H = health


def lt(day, h, m=0):
    """Yerel (İstanbul) saat → aware datetime."""
    y, mo, d = map(int, day.split("-"))
    return dt.datetime(y, mo, d, h, m, tzinfo=TR)


def cd(t):
    return t.timestamp() - COREDATA_EPOCH


MIHOME, ZEPP, PHONE = 1, 2, 3


class HealthDB:
    """healthdb_secure.sqlite + healthdb.sqlite sentetik kopyası (gerçek tablo/sütun adları)."""

    def __init__(self, root, schema):
        root.mkdir(parents=True, exist_ok=True)
        self.schema = schema
        m = sqlite3.connect(root / "healthdb.sqlite")
        m.execute("CREATE TABLE sources (ROWID INTEGER PRIMARY KEY, uuid BLOB, bundle_id TEXT, name TEXT, "
                  "source_options INTEGER, local_device INTEGER, product_type TEXT, deleted INTEGER)")
        m.executemany("INSERT INTO sources (ROWID, bundle_id, name) VALUES (?,?,?)",
                      [(11, "com.xiaomi.mihome", "Mi Home"), (12, "com.huami.watch", "Zepp"),
                       (13, "com.apple.health.00000000-0000-0000-0000-000000000000", "iPhone")])
        m.commit()
        m.close()
        c = self.c = sqlite3.connect(root / "healthdb_secure.sqlite")
        c.executescript("""
            CREATE TABLE samples (data_id INTEGER PRIMARY KEY, start_date REAL, end_date REAL, data_type INTEGER);
            CREATE TABLE quantity_samples (data_id INTEGER PRIMARY KEY, quantity REAL, original_quantity REAL,
                                           original_unit INTEGER);
            CREATE TABLE category_samples (data_id INTEGER PRIMARY KEY, value INTEGER);
            CREATE TABLE objects (data_id INTEGER PRIMARY KEY AUTOINCREMENT, uuid BLOB, provenance INTEGER NOT NULL,
                                  type INTEGER, creation_date REAL);
            CREATE TABLE data_provenances (ROWID INTEGER PRIMARY KEY, sync_provenance INTEGER, origin_product_type TEXT,
                                           origin_build TEXT, local_product_type TEXT, local_build TEXT,
                                           source_id INTEGER, device_id INTEGER, contributor_id INTEGER,
                                           source_version TEXT, tz_name TEXT);
            CREATE TABLE unit_strings (ROWID INTEGER PRIMARY KEY, unit_string TEXT);""")
        if schema == "new":   # iOS 16+
            c.executescript("""
                CREATE TABLE workouts (data_id INTEGER PRIMARY KEY, total_distance REAL, goal_type INTEGER, goal REAL);
                CREATE TABLE workout_activities (ROWID INTEGER PRIMARY KEY, owner_id INTEGER,
                    is_primary_activity INTEGER, activity_type INTEGER, location_type INTEGER, start_date REAL,
                    end_date REAL, duration REAL);""")
        else:
            c.execute("CREATE TABLE workouts (data_id INTEGER PRIMARY KEY, activity_type INTEGER, duration REAL, "
                      "total_energy_burned REAL, total_distance REAL)")
        c.executemany("INSERT INTO data_provenances (ROWID, source_id, tz_name) VALUES (?,?,?)",
                      [(MIHOME, 11, "Europe/Istanbul"), (ZEPP, 12, "Europe/Istanbul"), (PHONE, 13, "Europe/Istanbul")])
        self.units = {}
        self.n = 0

    def _sample(self, a, b, dtype, prov):
        self.n += 1
        self.c.execute("INSERT INTO samples VALUES (?,?,?,?)", (self.n, cd(a), cd(b or a), dtype))
        self.c.execute("INSERT INTO objects (data_id, provenance, type) VALUES (?,?,1)", (self.n, prov))
        return self.n

    def q(self, t, dtype, value, unit, prov, quantity=None):
        uid = None
        if unit:
            uid = self.units.setdefault(unit, len(self.units) + 1)
            self.c.execute("INSERT OR IGNORE INTO unit_strings VALUES (?,?)", (uid, unit))
        i = self._sample(t, t, dtype, prov)
        self.c.execute("INSERT INTO quantity_samples VALUES (?,?,?,?)",
                       (i, quantity if quantity is not None else value, value if unit else None, uid))

    def sleep(self, a, b, value, prov):
        i = self._sample(a, b, H.SLEEP, prov)
        self.c.execute("INSERT INTO category_samples VALUES (?,?)", (i, value))

    def workout(self, a, b, activity, prov):
        i = self._sample(a, b, H.WORKOUT, prov)
        dur = (b - a).total_seconds()
        if self.schema == "new":
            self.c.execute("INSERT INTO workouts (data_id) VALUES (?)", (i,))
            self.c.execute("INSERT INTO workout_activities (owner_id, is_primary_activity, activity_type, start_date,"
                           " end_date, duration) VALUES (?,1,?,?,?,?)", (i, activity, cd(a), cd(b), dur))
        else:
            self.c.execute("INSERT INTO workouts (data_id, activity_type, duration) VALUES (?,?,?)",
                           (i, activity, dur))

    def close(self):
        self.c.commit()
        self.c.close()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CFV_DATA", str(tmp_path / "data"))
    c = db.connect(db.init_db(tmp_path / "t.sqlite"))
    return c


def seed_zepp(conn, tmp_path):
    """zepp.py'nin (uygulama yedeği) 2026-09-10 gününü, gecesini ve bir egzersizini kapsadığı durum."""
    z = Batch(conn, "zepp", tmp_path / "zepp", "test")
    z.event("day/2026-09-10", "zepp_day", lt("2026-09-10", 0), day="2026-09-10", payload={"steps": 9000})
    z.event("sleep/x", "sleep", lt("2026-09-09", 23), end=lt("2026-09-10", 7), day="2026-09-10")
    z.event("workout/x", "workout", lt("2026-09-10", 10, 5))


def build(root, schema):
    h = HealthDB(root, schema)
    d = "2026-11-03"
    # tartı (Mi Home): aynı dakikada dört örnek → tek ölçüm
    for dtype, v, u in [(H.BODY_MASS, 72.4, "kg"), (H.BODY_FAT, 0.215, "%"), (H.BMI, 22.1, "count"),
                        (H.LEAN_MASS, 56.8, "kg")]:
        h.q(lt(d, 8), dtype, v, u, MIHOME)
    h.q(lt("2026-11-04", 8), H.BODY_MASS, 160, "lb", PHONE)            # elle, lb
    h.q(lt(d, 9), H.LEAN_MASS, 56.0, "kg", MIHOME)                     # yalnız yağsız kütle → ölçüm değil
    # 3 Kasım: iPhone 5000 adım, Zepp 6000 → Zepp seçilir
    h.q(lt(d, 10), H.STEPS, 3000, "count", PHONE)
    h.q(lt(d, 11), H.STEPS, 2000, "count", PHONE)
    h.q(lt(d, 12), H.STEPS, 6000, "count", ZEPP)
    for hh, bpm in [(7, 60), (13, 80), (20, 100)]:
        h.q(lt(d, hh), H.HEART_RATE, bpm, "count/min", ZEPP)
    h.q(lt(d, 14), H.HEART_RATE, 900, "count/min", ZEPP)              # aralık dışı
    h.q(lt(d, 6), H.RESTING_HR, 58, "count/min", ZEPP)
    h.q(lt(d, 22), H.RESTING_HR, 55, "count/min", ZEPP)              # günün son değeri
    h.q(lt(d, 18), H.EXERCISE_TIME, 30, "min", ZEPP)
    # 4 Kasım: birim bilgisi yok → kanonik değer + sezgi (nabız count/s)
    h.q(lt("2026-11-04", 9), H.STEPS, None, None, PHONE, quantity=1234)
    h.q(lt("2026-11-04", 9), H.HEART_RATE, None, None, PHONE, quantity=1.5)
    # 10 Eylül: zepp.py kapsıyor → gün, gece, egzersiz atlanır
    h.q(lt("2026-09-10", 12), H.STEPS, 4000, "count", PHONE)
    h.sleep(lt("2026-09-09", 23), lt("2026-09-10", 6), 3, ZEPP)
    h.workout(lt("2026-09-10", 10), lt("2026-09-10", 10, 30), 52, ZEPP)
    # 2→3 Kasım gecesi: Zepp evreleri + iPhone yatakta → Zepp seçilir
    p = "2026-11-02"
    for a, b, v in [((p, 23, 30), (d, 1, 30), 3), ((d, 1, 30), (d, 2, 30), 4), ((d, 2, 30), (d, 3, 30), 5),
                    ((d, 3, 30), (d, 3, 40), 2), ((d, 3, 40), (d, 6, 40), 3)]:
        h.sleep(lt(*a), lt(*b), v, ZEPP)
    h.sleep(lt(d, 3, 40), lt(d, 6, 40), 3, ZEPP)                       # aynı örnek iki kez → tek sayılır
    h.sleep(lt(p, 23), lt(d, 7), 0, PHONE)
    # 4→5 Kasım: yalnız iPhone "yatakta"
    h.sleep(lt("2026-11-04", 22), lt("2026-11-05", 6), 0, PHONE)
    # egzersiz: Zepp koşu, nabız penceresi 140/160 (Zepp) + iPhone 90 (aynı kaynak tercih edilir)
    h.workout(lt(d, 18), lt(d, 18, 40), 37, ZEPP)
    h.q(lt(d, 18, 10), H.HEART_RATE, 140, "count/min", ZEPP)
    h.q(lt(d, 18, 30), H.HEART_RATE, 160, "count/min", ZEPP)
    h.q(lt(d, 18, 20), H.HEART_RATE, 90, "count/min", PHONE)
    h.close()


@pytest.mark.parametrize("schema", ["new", "old"])
def test_health(conn, tmp_path, schema):
    seed_zepp(conn, tmp_path)
    root = tmp_path / "data" / "raw" / "iphone" / "extract-2026-11-15" / "HealthDomain" / "Health"
    build(root, schema)

    stats = health.ingest(conn, root)
    assert stats["out_of_range"] == {"hr": 1}
    assert stats["unit_fallbacks"] == 2
    assert stats["measurements"] == 2
    assert stats["days_covered_by_zepp"] == 1 and stats["nights_covered_by_zepp"] == 1
    assert stats["workouts_covered_by_zepp"] == 1
    assert stats["days"] == 2 and stats["sleeps"] == 2 and stats["workouts"] == 1
    assert stats["ranges"]["weight_kg"][0] == 72.4

    conn.row_factory = sqlite3.Row
    ev = {r["external_id"]: r for r in conn.execute(
        "SELECT e.* FROM event e JOIN source s ON s.id=e.source_id WHERE s.kind='health'")}
    pl = {k: json.loads(v["payload"]) for k, v in ev.items()}

    m = pl["measurement/mihome/2026-11-03T05:00"]
    assert m == {"weight_kg": 72.4, "fat_pct": 21.5, "lean_kg": 56.8, "bmi": 22.1, "source": "mihome"}
    assert ev["measurement/mihome/2026-11-03T05:00"]["title"] == "72,4 kg"
    assert pl["measurement/iphone/2026-11-04T05:00"]["weight_kg"] == 72.57

    day = pl["day/2026-11-03"]
    assert day["steps"] == 6000 and day["source"] == "zepp"
    assert day["steps_by_source"] == {"iphone": 5000, "zepp": 6000}
    assert (day["min_hr"], day["max_hr"], day["rest_hr"], day["active_min"]) == (60, 160, 55, 30)
    assert ev["day/2026-11-03"]["kind"] == "health_day" and ev["day/2026-11-03"]["local_date"] == "2026-11-03"
    assert pl["day/2026-11-04"]["steps"] == 1234 and pl["day/2026-11-04"]["max_hr"] == 90
    assert "day/2026-09-10" not in ev

    night = ev["sleep/2026-11-03"]
    sp = pl["sleep/2026-11-03"]
    assert sp["source"] == "zepp"
    assert (sp["light_min"], sp["deep_min"], sp["rem_min"], sp["awake_min"]) == (300, 60, 60, 10)
    assert sp["asleep_min"] == 420 and sp["total_min"] == 430
    assert night["start_utc"] == "2026-11-02T20:30:00Z" and night["title"] == "uyku 7s 00dk"
    inbed = pl["sleep/2026-11-05"]
    assert inbed["source"] == "iphone" and inbed["in_bed_min"] == 480 and inbed["asleep_min"] is None
    assert "sleep/2026-09-10" not in ev

    (wid,) = [k for k in ev if k.startswith("workout/")]
    w = pl[wid]
    assert (w["activity"], w["duration_min"], w["avg_hr"], w["max_hr"], w["source"]) == ("koşu", 40, 150, 160, "zepp")
    assert ev[wid]["title"] == "koşu 40 dk"

    # tekrar koşmak aynı olayları üretir
    before = conn.execute("SELECT external_id, payload FROM event WHERE source_id="
                          "(SELECT id FROM source WHERE kind='health') ORDER BY 1").fetchall()
    health.ingest(conn, root)
    after = conn.execute("SELECT external_id, payload FROM event WHERE source_id="
                         "(SELECT id FROM source WHERE kind='health') ORDER BY 1").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


@pytest.mark.parametrize("dtype,q,oq,unit,expected", [
    (H.BODY_MASS, None, 72.0, "kg", (72.0, False)),
    (H.BODY_MASS, 72000, None, None, (72.0, True)),
    (H.BODY_FAT, None, 0.2, "%", (20.0, False)),
    (H.BODY_FAT, 0.2, None, None, (20.0, True)),
    (H.HEART_RATE, None, 1.0, "count/s", (60.0, False)),
    (H.HEART_RATE, 72, None, None, (72, True)),
    (H.EXERCISE_TIME, None, 120, "s", (2.0, False)),
    (H.EXERCISE_TIME, 60, None, None, (1.0, True)),
    (H.STEPS, 10, None, None, (10, True)),
])
def test_convert(dtype, q, oq, unit, expected):
    v, fb = health.convert(dtype, q, oq, unit)
    assert (pytest.approx(v), fb) == (pytest.approx(expected[0]), expected[1])
