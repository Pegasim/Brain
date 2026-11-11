"""cfv-akis: snapshot seçimi, azalma denetimi, büyüme kaydı, gün kapanışı, uyarı tekrarı (sentetik veri)."""

import datetime as dt
import json
import sqlite3

import pytest

from cfv import db
from cfv.ingest import Batch, drop_other_snapshots, latest_dir, snapshot_of

UTC = dt.timezone.utc


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("CFV_DATA", str(tmp_path))
    return tmp_path


def test_latest_dir_requires_mark_for_new(data):
    root = data / "raw" / "iphone"
    for n in ("extract-2026-11-01", "extract-2026-11-20", "extract-2026-11-27"):
        (root / n).mkdir(parents=True)
    # 2026-11-20 işaretli, 11-27 yarım (işaretsiz) → 11-20 seçilir; eski işaretsiz geçerli
    (root / "extract-2026-11-20" / ".tamam").touch()
    assert latest_dir("extract", "extract-2026-11-01").name == "extract-2026-11-20"
    (root / "extract-2026-11-27" / ".tamam").touch()
    assert latest_dir("extract", "extract-2026-11-01").name == "extract-2026-11-27"
    assert snapshot_of(root / "extract-2026-11-27" / "AppDomain-x" / "Documents" / "123") == "2026-11-27"


def test_latest_dir_legacy_fallback(data):
    (data / "raw" / "iphone").mkdir(parents=True)
    assert latest_dir("extract", "extract-2026-11-01").name == "extract-2026-11-01"


def _db(path):
    conn = db.connect(path)
    conn.executescript(db.schema_sql())
    return conn


def test_drop_other_snapshots(data):
    conn = _db(data / "t.sqlite")
    t = dt.datetime(2026, 1, 1, tzinfo=UTC)
    with conn:
        b1 = Batch(conn, "whatsapp", data / "raw/iphone/extract-2026-11-01/x.sqlite", "t")
        b1.event("m1", "whatsapp", t)
        b2 = Batch(conn, "whatsapp", data / "raw/iphone/extract-2026-11-20/x.sqlite", "t")
        b2.event("m1", "whatsapp", t)
        assert drop_other_snapshots(conn, "whatsapp", b2.source_id) == 1
    assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 1


def test_drops():
    from akis.gunluk import drops
    before = {"whatsapp": 1000, "gmail": 50, "calendar-google": 1000, "zepp": 5}
    assert drops(before, {"whatsapp": 995, "gmail": 41, "calendar-google": 960, "zepp": 0}, set()) == []
    out = drops(before, {"whatsapp": 980, "gmail": 39, "calendar-google": 940, "zepp": 5}, {"notes"})
    assert [o.split(":")[0] for o in out] == ["calendar-google", "gmail", "whatsapp", "notes"]


def test_buyume(data):
    from akis import buyume
    assert buyume.append({"a": 10, "b": 5}, {"vektor": 3}, dt.date(2026, 11, 10))
    assert buyume.append({"a": 10, "b": 5}, {"vektor": 3}, dt.date(2026, 11, 10)) is None  # aynı gün tek satır
    line = buyume.append({"a": 12, "b": 5, "c": 1}, {"vektor": 4}, dt.date(2026, 11, 11))
    f = line.rstrip("\n").split("\t")
    assert f[:3] == ["2026-11-11", "18", "+3"]
    assert "a=12(+2)" in f[3] and "c=1(+1)" in f[3] and "vektor=4(+1)" in f[4]
    rs = buyume.rows()
    assert [r["toplam"] for r in rs] == [15, 18]
    assert "a+2" in buyume.table()


def test_gun_close_and_revise(data):
    from akis import gun
    p = data / "cfv.sqlite"
    conn = _db(p)
    with conn:
        b = Batch(conn, "zepp", data / "raw/x", "t")
        b.event("d1", "zepp_day", dt.datetime(2026, 11, 8, 10, tzinfo=UTC), payload={"steps": 100})
    conn.close()
    now = dt.datetime(2026, 11, 10, 4, tzinfo=UTC)  # İstanbul 07:00 → dün 11-09
    r = gun.close_days(p, now)
    assert r["dun"] == "2026-11-09" and r["kapanan"] == ["2026-11-08", "2026-11-09"] and r["revize"] == []
    assert gun.close_days(p, now)["kapanan"] == []
    # geç gelen saat verisi: aynı gün, sayı aynı ama içerik değişti → revizyon 2
    conn = db.connect(p)
    with conn:
        b = Batch(conn, "zepp", data / "raw/x", "t")
        b.event("d1", "zepp_day", dt.datetime(2026, 11, 8, 10, tzinfo=UTC), payload={"steps": 9000})
    conn.close()
    r = gun.close_days(p, now + dt.timedelta(days=1))
    assert r["kapanan"] == ["2026-11-10"] and r["revize"] == ["2026-11-08"]
    with sqlite3.connect(gun.path()) as c:
        assert c.execute("SELECT revizyon FROM gun WHERE tarih='2026-11-08'").fetchone()[0] == 2
        assert c.execute("SELECT count(*) FROM gun_degisim").fetchone()[0] == 4


def test_alert_reminder(data):
    from akis import tazelik
    t = dt.datetime(2026, 11, 10, 5, tzinfo=UTC)
    p = [("foto-cekim", "foto eski")]
    assert tazelik.due(p, t) == ["foto eski"]
    assert tazelik.due(p, t + dt.timedelta(days=1)) == []
    assert tazelik.due(p, t + dt.timedelta(days=3)) == ["foto eski"]
    assert tazelik.due([], t + dt.timedelta(days=4)) == []          # düzeldi → durumdan düşer
    assert tazelik.due(p, t + dt.timedelta(days=5)) == ["foto eski"]  # yeniden çıktı → hemen
    assert json.loads(tazelik.state_path().read_text())["foto-cekim"]["metin"] == "foto eski"


def test_pending_fetchers(monkeypatch):
    from akis import gunluk
    since = 1000.0
    st = {"cfv-icloudpd@gunluk.service": {"state": "inactive", "result": "success", "start": 500.0},
          "cfv-icloudpd@tam.service": {"state": "activating", "result": "success", "start": 1200.0},
          "cfv-gmail-sync.service": {"state": "inactive", "result": "success", "start": None},
          "cfv-gcal-sync.service": {"state": "inactive", "result": "success", "start": 900.0}}
    monkeypatch.setattr(gunluk, "unit_states", lambda units: st)
    out = gunluk.pending_fetchers(since)
    assert out == ["foto: cfv-icloudpd@tam.service sürüyor",
                   "gmail: bugün henüz başlamadı (cfv-gmail-sync.service)"]
    st["cfv-icloudpd@tam.service"]["state"] = "inactive"
    st["cfv-gmail-sync.service"]["start"] = 1100.0
    assert gunluk.pending_fetchers(since) == []


def test_gun_ignores_dream(data):
    from akis import gun
    p = data / "cfv.sqlite"
    conn = _db(p)
    t = dt.datetime(2026, 11, 8, 10, tzinfo=UTC)
    with conn:
        Batch(conn, "zepp", data / "raw/x", "t").event("d1", "zepp_day", t)
    conn.close()
    now = dt.datetime(2026, 11, 10, 4, tzinfo=UTC)
    gun.close_days(p, now)
    conn = db.connect(p)
    with conn:  # sabah gelen rüya: aynı güne düşer ama revizyon yaratmaz, sayılarda görünmez
        Batch(conn, "dream", data / "raw/ruya", "t").event("ruya-2026-11-08", "dream", t)
    conn.close()
    assert gun.close_days(p, now)["revize"] == []
    with sqlite3.connect(gun.path()) as c:
        assert "dream" not in c.execute("SELECT kaynak_sayilari FROM gun WHERE tarih='2026-11-08'").fetchone()[0]
