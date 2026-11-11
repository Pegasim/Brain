"""Ortak altyapı testleri — tamamen SENTETİK veri."""

import json
import sqlite3
from pathlib import Path

import pytest

from common import baselines, data, evaluate, targets
from common import sessions as S

SCHEMA = Path(__file__).resolve().parents[2] / "cfv" / "schema.sql"


def _ev(conn, kind, start, day, payload=None, title=None, end=None, quality="exact", people=()):
    cur = conn.execute(
        "INSERT INTO event (source_id, external_id, kind, start_utc, end_utc, local_date, time_quality,"
        " title, payload) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"{kind}-{start}-{title}", kind, start, end, day, quality, title, json.dumps(payload or {})))
    for p, role in people:
        conn.execute("INSERT OR IGNORE INTO person (id, display_name) VALUES (?, 'x')", (p,))
        conn.execute("INSERT INTO event_person VALUES (?, ?, ?, 1.0)", (cur.lastrowid, p, role))
    return cur.lastrowid


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "cfv.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA.read_text())
    conn.execute("INSERT INTO source VALUES (1, 'test', 'x', NULL, 'now', 'test')")
    dm = {"chat_jid": "a@s", "chat_type": "dm", "type": "text"}
    grp = {"chat_jid": "g@g", "chat_type": "group", "type": "text"}
    # 2025-03-01: DM'de 10:00-10:20 bir oturum (benden 1), 11:00'de yeni oturum (cevapsız)
    _ev(conn, "whatsapp", "2025-03-01T10:00:00Z", "2025-03-01", dm | {"from_me": 0}, "A", people=[(7, "counterpart")])
    _ev(conn, "whatsapp", "2025-03-01T10:20:00Z", "2025-03-01", dm | {"from_me": 1}, "A", people=[(7, "counterpart")])
    _ev(conn, "whatsapp", "2025-03-01T11:00:00Z", "2025-03-01", dm | {"from_me": 0}, "A", people=[(7, "counterpart")])
    _ev(conn, "whatsapp", "2025-03-01T11:01:00Z", "2025-03-01", dm | {"type": "system"}, "A")
    # grupta benden mesaj → chats'e girer, people'a girmez
    _ev(conn, "whatsapp", "2025-03-02T09:00:00Z", "2025-03-02", grp | {"from_me": 0}, "G", people=[(8, "sender")])
    _ev(conn, "whatsapp", "2025-03-02T09:05:00Z", "2025-03-02", grp | {"from_me": 1}, "G")
    for i in range(300):  # kapsam eşiği (ay başına 300) aşılsın
        _ev(conn, "whatsapp", f"2025-03-03T{i // 60:02d}:{i % 60:02d}:00Z", "2025-03-03", grp | {"from_me": 0}, "G")
    _ev(conn, "whatsapp", "2025-03-05T09:00:00Z", "2025-03-05", grp | {"from_me": 0}, "G")
    ph = _ev(conn, "photo", "2025-03-01T12:00:00Z", "2025-03-01", title="p1")
    ss = _ev(conn, "photo", "2025-03-01T12:01:00Z", "2025-03-01", title="p2")
    conn.execute("INSERT INTO tag VALUES (?, 'screenshot')", (ss,))
    _ev(conn, "zepp_day", "2025-03-01T00:00:00Z", "2025-03-01", {"steps": 5000}, quality="day")
    _ev(conn, "sleep", "2025-03-01T21:00:00Z", "2025-03-02", {"asleep_min": 420}, end="2025-03-02T04:00:00Z")
    _ev(conn, "calendar", "2025-03-02T07:00:00Z", "2025-03-02", {"all_day": 0, "created": "2025-02-20T00:00:00Z"},
        "ders", end="2025-03-02T09:00:00Z")
    _ev(conn, "calendar", "2025-03-02T12:00:00Z", "2025-03-02", {"all_day": 0, "created": "2025-03-02T10:00:00Z"},
        "sonradan", end="2025-03-02T13:00:00Z")
    conn.commit()
    conn.close()
    return path


def test_sessions(db):
    with sqlite3.connect(db) as conn:
        s = S.build(conn)
    a = [x for x in s if x["chat_jid"] == "a@s"]
    assert [(x["n_msgs"], x["n_me"], x["people"]) for x in a] == [(2, 1, [7]), (1, 0, [7])]


def test_build_and_targets(db, tmp_path, monkeypatch):
    monkeypatch.setattr(targets, "RUN", 1)
    monkeypatch.setitem(targets.MIN, "whatsapp", (300, 1))
    monkeypatch.setitem(targets.MIN, "photos", (1, 0))
    meta = data.build(db, tmp_path / "common.sqlite")
    assert meta["first_day"] == "2025-03-01" and meta["last_day"] == "2025-03-04"
    days = data.load_days(tmp_path / "common.sqlite")
    d1, d2, d4 = days["2025-03-01"], days["2025-03-02"], days["2025-03-04"]
    assert (d1["msgs_sent"], d1["msgs_recv"], d1["sessions"], d1["sessions_me"]) == (1, 2, 2, 1)
    assert d1["people"] == ["7"] and d1["chats"] == ["a@s"]
    assert d2["people"] == [] and d2["chats"] == ["g@g"]
    assert (d1["photos"], d1["screenshots"], d1["steps"]) == (1, 1, 5000)
    assert d2["sleep_min"] == 420 and "steps" not in d2  # saat yoksa NULL, 0 değil
    assert d4["msgs_sent"] == 0  # kapsam içinde sessiz gün = 0
    assert "2025-03-05" not in days  # son gün yarım sayılır


def test_known_excludes_later_calendar(db):
    from common.targets import known
    with sqlite3.connect(db) as conn:
        k = known(conn, "2025-03-02")
    assert [c["title"] for c in k["calendar"]] == ["ders"]


def test_baselines_walk_forward():
    by_day = {f"2025-01-{d:02d}": {"msgs_sent": d, "people": ["1"] if d % 7 == 1 else []} for d in range(1, 30)}
    hist = data.history(by_day, "2025-01-29")
    assert "2025-01-29" not in hist
    assert baselines.yesterday(hist, "2025-01-29")["msgs_sent"] == 28
    p = baselines.dow4(hist, "2025-01-29")  # 22, 15, 8, 1
    assert p["msgs_sent"] == 11.5 and p["people"] == ["1"]


def test_score():
    truth = {"d1": {"msgs_sent": 10, "people": ["a", "b"]}, "d2": {"msgs_sent": 0, "people": []}}
    pred = {"d1": {"msgs_sent": 7, "people": ["a"]}, "d2": {"msgs_sent": 1, "people": []}}
    assert evaluate.score(pred, truth, ["d1", "d2"], "msgs_sent")["mae"] == 2
    j = evaluate.score(pred, truth, ["d1", "d2"], "people")
    assert j["jaccard"] == 0.75 and j["recall"] == 0.75


def test_covered_months_gap(tmp_path, monkeypatch):
    """Dışa aktarma boşluğu olan ay NULL olmalı; başlangıç art arda RUN uygun ay ister."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA.read_text())
    conn.execute("INSERT INTO source VALUES (1, 'test', 'x', NULL, 'now', 'test')")
    monkeypatch.setitem(targets.MIN, "whatsapp", (2, 2))
    good, bad = [1, 2], [1]
    for m, days in [("01", bad), ("02", good), ("03", good), ("04", bad), ("05", good), ("06", good),
                    ("07", good), ("08", bad), ("09", good)]:
        for d in days:
            _ev(conn, "whatsapp", f"2025-{m}-{d:02d}T10:00:00Z", f"2025-{m}-{d:02d}", title=m)
    got = targets.covered_months(conn, "whatsapp", "2025-09-30")
    assert got == {"2025-05", "2025-06", "2025-07", "2025-09"}  # 02-03 RUN=3'e yetmez, 08 boşluk
    monkeypatch.setattr(targets, "RUN", 2)
    assert targets.covered_months(conn, "whatsapp", "2025-09-30") == {
        "2025-02", "2025-03", "2025-05", "2025-06", "2025-07", "2025-09"}


def test_crps():
    assert evaluate.crps([5], 2) == 3  # nokta tahminde CRPS = MAE
    assert evaluate.crps([0, 10], 5) == 5 - 2.5
    s = {"d1": [{"msgs_sent": x} for x in range(11)]}
    m = evaluate.score_samples(s, {"d1": {"msgs_sent": 5}}, ["d1"], "msgs_sent")
    assert m["cov80"] == 1 and m["n"] == 1
