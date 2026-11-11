"""Rüya günlüğü testleri — tamamen SENTETİK veri (gerçek rüya metni yok)."""

import datetime as dt
import json

import pytest

from cfv import db, ruya
from cfv.ingest import Batch
from cfv.sources import dream

JID = "120363000000000000@g.us"
U = dt.timezone.utc


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CFV_DATA", str(tmp_path))
    monkeypatch.setenv("CFV_RUYA_UYARI", "0")                    # testte bp uyarısı yok
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "ruya.json").write_text(json.dumps({"wa_jid": JID}))
    store = tmp_path / "wa" / "messages.jsonl"
    store.parent.mkdir()
    monkeypatch.setattr(ruya, "WA_STORE", store)
    monkeypatch.setattr(ruya.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "", "returncode": 0})())
    return tmp_path, store


def wa(store, mid, ts, text, jid=JID, from_me=True, file=None):
    r = {"ts": ts, "chatJid": jid, "chatType": "group", "chatName": "Rüya", "fromMe": from_me, "id": mid,
         "senderName": "Ben", "text": text}
    if file:
        r["file"] = str(file)
    with store.open("a") as f:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")


@pytest.mark.parametrize("ts,text,day,rest", [
    ("2026-11-11T04:30:00Z", "a", "2026-11-10", "a"),          # 07:30 yerel → önceki gece
    ("2026-11-10T22:30:00Z", "a", "2026-11-10", "a"),          # 11 Kas 01:30 yerel → 10 Kas
    ("2026-11-11T17:00:00Z", "a", "2026-11-10", "a"),          # akşam 20:00 yazılsa da geçen gece
    ("2026-11-11T20:59:00Z", "a", "2026-11-10", "a"),          # 23:59 yerel
    ("2026-11-11T21:00:00Z", "a", "2026-11-11", "a"),          # 12 Kas 00:00 yerel → 11 Kas
    ("2026-11-14T05:00:00Z", "12 kas: b", "2026-11-12", "b"),
    ("2026-11-14T05:00:00Z", "12 Kasım - b", "2026-11-12", "b"),
    ("2026-11-14T05:00:00Z", "2026-11-09: b", "2026-11-09", "b"),
    ("2026-01-02T05:00:00Z", "30 ara: b", "2025-12-30", "b"),  # yıl dönümü
    ("2026-11-14T05:00:00Z", "12 kişi vardı: b", "2026-11-13", "12 kişi vardı: b"),  # ay değil → önek yok
])
def test_night_rule(ts, text, day, rest):
    assert ruya.night_of(dt.datetime.fromisoformat(ts.replace("Z", "+00:00")), text) == (day, rest)


def test_collect_append_only_and_filters(env):
    root, store = env
    audio = store.parent / "M2-audio.ogg"
    audio.write_bytes(b"OggS-sentetik")
    wa(store, "M1", "2026-11-11T04:30:00Z", "sentetik rüya bir")
    wa(store, "M2", "2026-11-11T04:31:00Z", "[dosya] M2-audio.ogg", file=audio)
    wa(store, "X1", "2026-11-11T04:32:00Z", "başka grup", jid="999@g.us")
    wa(store, "X2", "2026-11-11T04:33:00Z", "başkası yazdı", from_me=False)
    assert ruya.collect_wa() == 2
    raw = root / "raw" / "ruya" / "wa" / "2026-11"
    assert sorted(p.name for p in raw.iterdir()) == ["M1.json", "M2.json", "M2.ogg"]
    before = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in raw.iterdir()}
    store.write_text(store.read_text().replace("sentetik rüya bir", "DEĞİŞTİ"))   # yeniden yazılan dosya (yeni inode)
    (root / "processed" / "ruya" / "okuma.json").write_text("{}")                  # baştan oku: ham yine dokunulmaz
    assert ruya.collect_wa() == 0
    assert {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in raw.iterdir()} == before
    wa(store, "M4", "2026-11-11T05:00:00Z", "sentetik sonra")       # artımlı: yalnız yeni satır
    with store.open("a") as f:
        f.write('{"ts": "2026-11-11T05:01:00Z", "chatJid": "' + JID + '", "fromMe": true, "id": "M5", "te')  # yarım
    assert ruya.collect_wa() == 1
    with store.open("a") as f:
        f.write('xt": "sentetik"}\n')
    assert ruya.collect_wa() == 1 and (raw / "M5.json").exists()
    log = (root / "logs" / "ruya.log").read_text()
    assert "sentetik" not in log and "M1" in log                  # loga metin girmez


def test_merge_no_recall_and_audio_wait(env):
    root, store = env
    audio = store.parent / "M3-audio.ogg"
    audio.write_bytes(b"OggS")
    wa(store, "M1", "2026-11-11T04:30:00Z", "sentetik a")
    wa(store, "M2", "2026-11-12T05:00:00Z", "Hatırlamıyorum.")
    wa(store, "M3", "2026-11-13T05:00:00Z", "[dosya] M3-audio.ogg", file=audio)
    post = lambda gid, durum, metin, at: (k / f"{gid}.json").write_text(json.dumps({"alindi_at": at, "govde": json.dumps(
        {"v": 1, "gonderim_id": gid, "uyku": [], "ruya": {"durum": durum, "metin": metin, "soruldu_at": at,
                                                           "cevaplandi_at": at}})}))
    k = root / "raw" / "sabah" / "2026-11"
    k.mkdir(parents=True)
    post("g1", "gordum", "sentetik b", "2026-11-11T07:10:00+03:00")
    post("g2", "atla", None, "2026-11-12T07:10:00+03:00")
    post("g3", "sorulmadi", None, "2026-11-12T10:10:00+03:00")
    post("g4", "gordum", "  ", "2026-11-12T07:20:00+03:00")
    (k / "bozuk.json").write_text("bozuk")
    ruya.collect_wa()
    ds = {d["day"]: d for d in ruya.dreams()}
    assert ds["2026-11-10"]["text"] == "sentetik b\n\nsentetik a" and ds["2026-11-10"]["recalled"] == 1
    assert sorted(p["src"] for p in ds["2026-11-10"]["parts"]) == ["kestirme", "wa"]
    assert ds["2026-11-11"]["recalled"] == 0 and len(ds["2026-11-11"]["parts"]) == 1  # atla/sorulmadi/boş kayıt değil
    assert not ds["2026-11-12"]["ready"]                          # ses yazıya dökülmeden yorum yok
    (root / "processed" / "ruya" / "transkript").mkdir(parents=True)
    (root / "processed" / "ruya" / "transkript" / "M3.txt").write_text("sentetik ses\n")
    d = {d["day"]: d for d in ruya.dreams()}["2026-11-12"]
    assert d["ready"] and d["text"] == "sentetik ses"


def _db(root):
    conn = db.connect()
    conn.executescript(db.schema_sql())
    b = Batch(conn, "calendar", root / "raw" / "cal", "test")
    t = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=U)
    past = b.event("c1", "calendar", t("2026-11-10T09:00:00"), title="GEÇMİŞ-SINAV")
    b.event("c2", "calendar", t("2026-11-11T09:00:00"), title="GELECEK-TOPLANTI", day="2026-11-10")
    b.event("c3", "calendar", t("2026-11-01T09:00:00"), title="ESKİ-GÜN")
    ali = b.person("ali@x", "Ali Sentetik", "email")
    b.link_person(past, ali, "attendee")
    return conn, past, ali


def test_context_is_past_only_and_eval_validation(env):
    root, store = env
    conn, past, ali = _db(root)
    wa(store, "M1", "2026-11-11T04:30:00Z", "sentetik qwxzebra rüyası")
    ruya.collect_wa()
    now = dt.datetime(2026, 11, 11, 4, 40, tzinfo=U)
    assert ruya.make_jobs(conn, now=now) == []                    # gün kapanmadı, 4 saat dolmadı
    keys = ruya.make_jobs(conn, now=now + dt.timedelta(hours=5))
    assert len(keys) == 1
    prompt = (ruya.jobs_dir() / f"{keys[0]}.in.md").read_text()
    assert "GEÇMİŞ-SINAV" in prompt and "GELECEK-TOPLANTI" not in prompt and "ESKİ-GÜN" not in prompt
    assert f"P{ali} Ali Sentetik" in prompt
    assert ruya.make_jobs(conn, now=now + dt.timedelta(hours=6)) == []   # aynı metin → yeni iş yok
    (ruya.jobs_dir() / f"{keys[0]}.json").write_text(json.dumps({
        "dusunce_izi": "x", "duygu": {"ana": "kaygı", "valence": -9, "yogunluk": 2},
        "kisiler": [{"ruyadaki": "Ali", "person": f"P{ali}", "guven": 0.9}, {"ruyadaki": "uydurma", "person": "P99999"}],
        "gunduz_baglantisi": [{"ref": f"#{past}", "guc": 0.8}, {"ref": "#123456", "guc": 1}],
        "temalar": ["sınav"], "onem_sinyali": []}))
    with conn:
        st = dream.ingest(conn)
    assert st == {"ruya": 1, "hatirlamiyorum": 0, "degerlendirme": 1, "kisi_onerisi": 1}
    ev = conn.execute("SELECT valence, persons, day_links FROM dream_eval").fetchone()
    assert ev[0] == -2
    assert [k["person"] for k in json.loads(ev[1])] == [f"P{ali}", None]
    links = json.loads(ev[2])
    assert len(links) == 1 and links[0]["olay"]["external_id"] == "c1"
    assert conn.execute("SELECT confidence FROM event_person WHERE role='dream'").fetchone()[0] == 0.6
    assert conn.execute("SELECT body FROM event WHERE kind='dream'").fetchone()[0] is None   # metin event'te yok
    assert conn.execute("SELECT count(*) FROM event_fts WHERE event_fts MATCH 'qwxzebra'").fetchone()[0] == 0

    # Tuna reddeder → öneri kalkar; geri alır → geri gelir
    ruya.cli(["karar", "2026-11-10", f"P{ali}", "hayir"])
    with conn:
        assert dream.ingest(conn)["kisi_onerisi"] == 0
    ruya.cli(["geri", "1"])
    ruya.cli(["karar", "2026-11-10", "Ali", "evet"])
    with conn:
        dream.ingest(conn)
    assert conn.execute("SELECT confidence FROM event_person WHERE role='dream'").fetchone()[0] == 1.0


def test_dream_role_not_counted_as_copresence(env):
    root, store = env
    conn, past, ali = _db(root)
    wa(store, "M1", "2026-11-12T04:30:00Z", "sentetik")
    ruya.collect_wa()
    with conn:
        dream.ingest(conn)
    did = conn.execute("SELECT id FROM event WHERE kind='dream'").fetchone()[0]
    conn.execute("INSERT INTO event_person VALUES (?,?,'dream',0.5)", (did, ali))
    d = ruya.dreams()[0]
    prompt, refs = ruya.build_prompt(conn, d)
    assert f"P{ali} Ali Sentetik (1/1)" in prompt                 # rüya satırı sayıma girmedi
    assert refs["P"][f"P{ali}"] == ["email", "ali@x"]


def test_private_kinds_skip_tree(env):
    root, store = env
    conn, *_ = _db(root)
    wa(store, "M1", "2026-11-12T04:30:00Z", "sentetik")
    ruya.collect_wa()
    with conn:
        dream.ingest(conn)
    from cfv import tree
    tree.build(conn, root / "timeline")
    assert not any("rüya" in p.read_text(errors="ignore") for p in (root / "timeline").rglob("*") if p.is_file())


def test_alert_never_sends_in_tests_and_marks_trial(env, monkeypatch):
    root, _ = env
    sent = []
    monkeypatch.setattr(ruya.subprocess, "run", lambda cmd, **k: sent.append(cmd))
    ruya.alert("x")                                               # pytest + CFV_RUYA_UYARI=0 → gönderilmez
    monkeypatch.delenv("CFV_RUYA_UYARI")
    ruya.alert("x")
    assert sent == []
    monkeypatch.delenv("PYTEST_CURRENT_TEST")                     # elle deneme gibi: başka CFV_DATA
    ruya.alert("x")
    assert sent[0][-1].startswith("DENEME x (log: ") and str(root.resolve()) in sent[0][-1]
