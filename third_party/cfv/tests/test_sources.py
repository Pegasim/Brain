"""Kaynak ayrıştırıcı testleri — tamamen SENTETİK veri."""

import datetime as dt
from pathlib import Path

import pytest

from cfv import db, search, tree
from cfv.ingest import Batch
from cfv.sources import calendar_ics, notes, photos
from cfv.sources.notes_proto import Line

FIX = Path(__file__).parent / "fixtures"


def B(text):  # kalın satır
    return Line(text, bold=True)


def C(text, done=False):
    return Line(text, style="checkbox", checked=done)


@pytest.mark.parametrize("text,idx", [
    ("pzt", 0), ("pazartesi", 0), ("salı", 1), ("sali", 1), ("çarş", 2), ("cars", 2),
    ("perş", 3), ("pers", 3), ("cuma", 4), ("cuma :-(", 4), ("cmt", 5), ("cumartesi", 5),
    ("pazar", 6), ("pzr", 6), ("perşe", 3), ("paz", 6), ("pzt-salı-çaşr", 0), ("OKUL", None), ("PROJE", None),
    ("market listesi", None), ("perşembe", 3),
])
def test_parse_day(text, idx):
    assert notes.parse_day(text) == idx


def test_parse_todo_note_weeks_days_and_year_wrap():
    lines = [B("Ödevler 2024"), Line(""),
             B("2"), B("salı"), C("ödev A", True), C("ödev B"),
             B("1 yeni sistem"), C("hafta işi"), B("cuma"), B("OKUL"), C("dosya hazırla", True),
             B("52"), B("pzt"), Line("düz satır", style="numbered")]
    weeks = notes.parse_todo_note(lines, 2024)
    assert [(w.year, w.week) for w in weeks] == [(2024, 2), (2024, 1), (2023, 52)]
    assert weeks[1].label == "yeni sistem"
    a, b = weeks[0].todos
    assert (a.text, a.done, a.day, a.date) == ("ödev A", True, 1, dt.date(2024, 1, 9))
    assert b.done is False
    week_level, okul = weeks[1].todos
    assert week_level.day is None and week_level.date == dt.date(2024, 1, 1)
    assert (okul.day, okul.section) == (4, "OKUL")
    (last,) = weeks[2].todos
    assert last.date == dt.date(2023, 12, 25) and last.done is None


def test_parse_todo_note_ignores_out_of_order_numbers():
    lines = [B("40"), C("x"), B("6937"), B("45"), C("y")]
    (w,) = notes.parse_todo_note(lines, 2025)
    assert w.week == 40
    assert [t.text for t in w.todos] == ["x", "y"]  # '45' bölüm başlığı sayılır
    assert w.todos[1].section == "45"


@pytest.mark.parametrize("name,expected", [
    ("IMG_0787.JPG", ("img_0787", "jpg", "original", None)),
    ("IMG_0787-5337221.JPG", ("img_0787", "jpg", "original", 5337221)),
    ("IMG_3469-adjusted.HEIC", ("img_3469", "heic", "adjusted", None)),
    ("IMG_1062_HEVC.MOV", ("img_1062", "mov", "live", None)),
    ("0a1b2c3d-1111-4222-8333-44445555aaaa.JPG",
     ("0a1b2c3d-1111-4222-8333-44445555aaaa", "jpg", "original", None)),
])
def test_photo_parse_name(name, expected):
    assert photos.parse_name(name) == expected


def test_fts_query_turkish():
    # ı/İ → i katlanır; büyük-küçük harf ve diğer aksanları FTS (unicode61) halleder
    assert search.fts_query("Işık İzmir") == '"Işik"* "izmir"*'
    assert search.fts_query('"robot kol" ders') == '"robot kol" "ders"*'


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CFV_DATA", str(tmp_path / "data"))
    return db.connect(db.init_db(tmp_path / "t.sqlite"))


def test_calendar_ics_recurrence_and_all_day(conn):
    stats = calendar_ics.ingest(conn, [FIX / "sample.ics"])
    assert stats["events"] == 5  # 1 + 3 tekrar + 1 tüm gün
    rows = conn.execute("SELECT local_date, title, time_quality FROM event ORDER BY start_utc").fetchall()
    assert [r[0] for r in rows if r[1] == "Robotik dersi"] == ["2025-03-03", "2025-03-10", "2025-03-17"]
    assert ("2025-03-15", "Yarışma günü", "day") in rows
    hits = search.search(conn, "dis hekim")
    assert hits and hits[0]["title"] == "Diş hekimi"


def test_reingest_replaces_events(conn):
    calendar_ics.ingest(conn, [FIX / "sample.ics"])
    calendar_ics.ingest(conn, [FIX / "sample.ics"])
    assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 5


def test_whatsapp_title_not_indexed_and_tree(conn, tmp_path):
    b = Batch(conn, "test", tmp_path / "x", "test")
    t = dt.datetime(2025, 9, 30, 10, 0, tzinfo=dt.timezone.utc)
    b.event("m1", "whatsapp", t, title="Örnek Grup", body="👍", payload={"sender": "ben"})
    b.event("m2", "whatsapp", t, title="Örnek Grup", body="robot geldi", payload={"sender": "ben"})
    b.event("t1", "todo", t, title="robot topla", payload={"done": True, "order": 1})
    assert {h["body"] if "body" in h else h["snippet"] for h in search.search(conn, "robot")} \
        == {"robot geldi", "robot topla"}
    (w,) = search.weeks(conn, "robot")
    assert w["week"] == "2025-W40" and w["hits"] == 2

    out = tmp_path / "timeline"
    tree.build(conn, out)
    wdir = out / "2025" / "09-Eylül" / "W40 (29-30)"
    text = (wdir / "README.md").read_text()
    assert "Salı 30 Eylül 2025" in text and "[x] robot topla" in text
    assert "robot geldi" in (wdir / "whatsapp.md").read_text()
    tree.build(conn, out)  # tekrar üretilebilir
    (out / ".cfv-generated").unlink()
    with pytest.raises(RuntimeError):
        tree.build(conn, out)  # yabancı klasörün üstüne yazmaz


def test_turkish_search_end_to_end(conn, tmp_path):
    b = Batch(conn, "test", tmp_path / "y", "test")
    b.event("n1", "note", dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc), title="IŞIK sensörü", body="ölçüm")
    for q in ("ışık", "IŞIK", "isik", "Işık sens", "olcum"):
        assert len(search.search(conn, q)) == 1, q


def test_calendar_gcal_snapshot_and_diff(conn, tmp_path):
    import json
    from cfv import gcal_sync
    from cfv.sources import calendar_gcal
    ev = [
        {"CalendarID": "me@example.com", "id": "a1", "etag": "1", "summary": "Robot montaj",
         "start": {"dateTime": "2025-03-03T18:00:00+03:00"}, "end": {"dateTime": "2025-03-03T20:00:00+03:00"},
         "location": "Atölye", "attendees": [{"email": "me@example.com", "self": True}, {"email": "x@example.com"}]},
        {"CalendarID": "me@example.com", "id": "a2", "etag": "1", "summary": "Kamp",
         "start": {"date": "2025-03-15"}, "end": {"date": "2025-03-17"}},
        {"CalendarID": "me@example.com", "id": "a3", "status": "cancelled", "summary": "iptal",
         "start": {"date": "2025-03-16"}},
        {"CalendarID": "tr.turkish#holiday@group.v.calendar.google.com", "id": "h", "summary": "Bayram",
         "start": {"date": "2025-03-30"}, "end": {"date": "2025-03-31"}},
    ]
    p = tmp_path / "events.json"
    p.write_text(json.dumps({"fetched_at": "2025-03-01T00:00:00Z", "events": ev,
                             "calendars": [{"id": "me@example.com", "summary": "Ben"}]}))
    assert calendar_gcal.ingest(conn, p) == {"events": 2, "skipped": 2}
    rows = conn.execute("SELECT local_date, title, time_quality, tz_offset_min, start_utc FROM event "
                        "ORDER BY start_utc").fetchall()
    assert rows[0] == ("2025-03-03", "Robot montaj", "exact", 180, "2025-03-03T15:00:00Z")
    assert rows[1][:3] == ("2025-03-15", "Kamp", "day")
    assert search.search(conn, "atolye")[0]["title"] == "Robot montaj"

    new = [dict(ev[0], etag="2"), ev[3], {**ev[1], "id": "a4"}]
    d = gcal_sync.diff(ev, new)
    assert d["added"] == ["me@example.com/a4"]
    assert d["changed"] == ["me@example.com/a1"]
    assert [e["id"] for e in d["removed"]] == ["a2", "a3"]


def test_gmail_reader(conn, tmp_path):
    import json
    from cfv.sources import gmail
    acc = tmp_path / "me@example.com"
    acc.mkdir()
    msgs = [
        {"id": "g1", "thread": "t1", "time": "2025-04-01T09:00:00Z", "labels": ["INBOX", "CATEGORY_UPDATES"],
         "from": "Ayşe Örnek <ayse@example.com>", "to": "me@example.com", "cc": None,
         "subject": "Robot toplantısı", "snippet": "yarın atölyede"},
        {"id": "g2", "thread": "t1", "time": "2025-04-01T10:00:00Z", "labels": ["SENT"],
         "from": "Ben <me@example.com>", "to": "ayse@example.com", "cc": "Can <can@example.com>",
         "subject": "Re: Robot toplantısı", "snippet": "tamam"},
        {"id": "g3", "thread": "t2", "time": "2025-04-02T10:00:00Z", "labels": ["SPAM"],
         "from": "x@example.com", "to": "me@example.com", "subject": "kazandınız", "snippet": "..."},
    ]
    (acc / "messages.jsonl").write_text("".join(json.dumps(m) + "\n" for m in msgs))
    assert gmail.ingest(conn, tmp_path) == {"emails": 2, "skipped": 1}
    rows = conn.execute("SELECT external_id, kind, title, body, json_extract(payload,'$.direction') "
                        "FROM event ORDER BY start_utc").fetchall()
    assert [r[:3] for r in rows] == [("g1", "email", "Robot toplantısı"), ("g2", "email", "Re: Robot toplantısı")]
    assert rows[0][3] == "Kimden: Ayşe Örnek <ayse@example.com>\nKime: me@example.com\nyarın atölyede"
    assert [r[4] for r in rows] == ["in", "out"]
    links = conn.execute("SELECT e.external_id, pa.alias, p.display_name, ep.role FROM event_person ep "
                         "JOIN event e ON e.id=ep.event_id JOIN person p ON p.id=ep.person_id "
                         "JOIN person_alias pa ON pa.person_id=p.id ORDER BY 1, 2").fetchall()
    assert links == [("g1", "ayse@example.com", "Ayşe Örnek", "sender"),
                     ("g2", "ayse@example.com", "Ayşe Örnek", "recipient"),
                     ("g2", "can@example.com", "Can", "recipient")]
    assert search.search(conn, "atolyede")[0]["title"] == "Robot toplantısı"


# --- photos: sentetik Photos.sqlite + icloud-sync kütüphanesi -------------------------------

_CORE_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)
_TR = dt.timezone(dt.timedelta(hours=3))


def _photos_db(path: Path, assets):
    """assets: (pk, uuid, kind, taken, fname, fsize, favorite). Yalnız photos.ingest'in okuduğu kolonlar."""
    import sqlite3
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE ZPERSON(Z_PK INTEGER PRIMARY KEY, ZFULLNAME, ZDISPLAYNAME, ZPERSONUUID, ZMERGETARGETPERSON);
        CREATE TABLE ZDETECTEDFACE(ZASSETFORFACE, ZPERSONFORFACE);
        CREATE TABLE ZGENERICALBUM(Z_PK INTEGER PRIMARY KEY, ZKIND, ZTRASHEDSTATE, ZTITLE);
        CREATE TABLE Z_30ASSETS(Z_3ASSETS, Z_30ALBUMS);
        CREATE TABLE ZMOMENT(Z_PK INTEGER PRIMARY KEY, ZTITLE, ZSUBTITLE);
        CREATE TABLE ZASSETDESCRIPTION(Z_PK INTEGER PRIMARY KEY, ZLONGDESCRIPTION);
        CREATE TABLE ZASSET(Z_PK INTEGER PRIMARY KEY, ZUUID, ZKIND, ZKINDSUBTYPE, ZDATECREATED, ZLATITUDE,
            ZLONGITUDE, ZFAVORITE, ZHIDDEN, ZWIDTH, ZHEIGHT, ZDURATION, ZMOMENT, ZUNIFORMTYPEIDENTIFIER, ZTRASHEDSTATE);
        CREATE TABLE ZADDITIONALASSETATTRIBUTES(ZASSET, ZORIGINALFILENAME, ZORIGINALFILESIZE, ZTIMEZONEOFFSET,
            ZTIMEZONENAME, ZASSETDESCRIPTION);
        INSERT INTO ZPERSON VALUES (1, 'Ayşe Örnek', 'Ayşe', 'P-1', NULL);
    """)
    for pk, uuid, kind, taken, fname, fsize, fav in assets:
        c.execute("INSERT INTO ZASSET VALUES (?,?,?,0,?,NULL,NULL,?,0,10,10,?,NULL,NULL,0)",
                  (pk, uuid, kind, (taken - _CORE_EPOCH).total_seconds(), fav, 3.0 if kind == 1 else 0))
        c.execute("INSERT INTO ZADDITIONALASSETATTRIBUTES VALUES (?,?,?,10800,'Europe/Istanbul',NULL)",
                  (pk, fname, fsize))
    c.execute("INSERT INTO ZDETECTEDFACE VALUES (1, 1)")
    c.commit()
    c.close()
    return path


def _media_file(lib: Path, rel: str, taken: dt.datetime | None, rating: int | None = None):
    p = lib / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    if taken is not None:
        extra = f"<xmp:Rating>{rating}</xmp:Rating>" if rating else ""
        p.with_name(p.name + ".xmp").write_text(
            f"<x><exif:GPSLatitude>41.0</exif:GPSLatitude><exif:GPSLongitude>29.0</exif:GPSLongitude>"
            f"<xmp:CreateDate>{taken.astimezone(_TR):%Y-%m-%dT%H:%M:%S%z}</xmp:CreateDate>{extra}</x>")
    return p


def test_photos_reingest_paths_copies_videos_snapshots(conn, tmp_path):
    t1 = dt.datetime(2025, 1, 5, 10, 0, tzinfo=dt.timezone.utc)
    t2 = dt.datetime(2025, 1, 6, 10, 0, tzinfo=dt.timezone.utc)
    t3 = dt.datetime(2025, 1, 7, 10, 0, tzinfo=dt.timezone.utc)
    t_new = dt.datetime(2025, 2, 1, 12, 0, tzinfo=dt.timezone.utc)  # snapshot'tan sonra
    assets = [(1, "A-1", 0, t1, "IMG_0001.JPG", 5000, 0),
              (2, "A-2", 1, t2, "IMG_0002.MOV", 1000000, 0),        # aynı adlı iki farklı video (gerçek çakışma)
              (3, "A-3", 1, t3, "IMG_0002.MOV", 2000000, 1)]
    raw_iphone = tmp_path / "data" / "raw" / "iphone"
    _photos_db(raw_iphone / "photos-db-2025-01-10" / "Photos.sqlite", assets)

    # 1) eski laptop yolu: önce laptop kütüphanesiyle içe aktarılmış DB
    laptop = tmp_path / "laptop" / "icloud-sync"
    _media_file(laptop, "2025/01/IMG_0001.JPG", t1)
    photos.ingest(conn, library=laptop)
    assert conn.execute("SELECT count(*) FROM media WHERE path LIKE ?", (f"{laptop}%",)).fetchone()[0] == 1

    # sunucu kütüphanesi: asıl + exif kopyası '-5000' + karantina (gizli klasör) + iki video + yeni foto
    srv = tmp_path / "srv" / "icloud-sync"
    _media_file(srv, "2025/01/IMG_0001.JPG", t1)
    _media_file(srv, "2025/01/IMG_0001-5000.JPG", t1)
    _media_file(srv, "2025/01/.kopyalar-karantina-x/IMG_0001-5000.JPG", t1)
    _media_file(srv, "2025/01/IMG_0002.MOV", t2)
    _media_file(srv, "2025/01/IMG_0002-2000000.MOV", t3)
    _media_file(srv, "2025/02/new-uuid.JPG", t_new, rating=5)
    stats = photos.ingest(conn, library=srv)

    paths = dict(conn.execute("SELECT e.external_id, m.path FROM media m JOIN event e ON e.id=m.event_id "
                              "WHERE m.role='original'").fetchall())
    # laptop yolu kalmadı
    assert conn.execute("SELECT count(*) FROM media WHERE path LIKE ?", (f"{laptop}%",)).fetchone()[0] == 0
    # 2) soneksiz asıl seçilir; '-5000' kopyası 'alt' olur; karantina hiç görülmez
    assert paths["A-1"] == str(srv / "2025/01/IMG_0001.JPG")
    assert conn.execute("SELECT count(*) FROM media WHERE path LIKE '%karantina%'").fetchone()[0] == 0
    assert conn.execute("SELECT role FROM media WHERE path=?",
                        (str(srv / "2025/01/IMG_0001-5000.JPG"),)).fetchone() == ("alt",)
    # 3) videolar dosyaya bağlı; aynı adlı çakışma zamana/boyuta göre doğru dosyaya
    assert paths["A-2"] == str(srv / "2025/01/IMG_0002.MOV")
    assert paths["A-3"] == str(srv / "2025/01/IMG_0002-2000000.MOV")
    # 4) snapshot sonrası dosya: XMP'den asgari olay, favori + icloud-only
    assert stats["icloud_only"] == 1
    tags = {t for (t,) in conn.execute(
        "SELECT t.tag FROM tag t JOIN event e ON e.id=t.event_id WHERE e.external_id='file:2025/02/new-uuid.JPG'")}
    assert tags == {"icloud-only", "favorite"}
    assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 4

    # 5) yeni snapshot: eski snapshot olayları silinir (çift yok), yeni foto artık DB'den gelir
    _photos_db(raw_iphone / "photos-db-2025-02-05" / "Photos.sqlite",
               assets + [(4, "A-4", 0, t_new, "new-uuid.JPG", 1, 1)])
    assert photos.latest_snapshot().parent.name == "photos-db-2025-02-05"
    stats = photos.ingest(conn, library=srv)
    assert stats.get("icloud_only", 0) == 0
    assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 4
    assert conn.execute("SELECT count(*) FROM tag WHERE tag='icloud-only'").fetchone()[0] == 0
    assert conn.execute("SELECT count(DISTINCT external_id) FROM event").fetchone()[0] == 4


# --- photos.match_files: ad / uzantı / zaman+boyut eşlemesi (saf, disk yok) ---------------------------
_T0 = dt.datetime(2025, 3, 10, 12, 0, tzinfo=dt.timezone.utc)


def _A(uuid, fname, fsize, taken=_T0, video=False):
    stem, ext, _, _ = photos.parse_name(fname)
    return photos._Asset(uuid, 0, taken, fname, stem, ext, fsize, video, None, None, None)


def _F(name, size, taken=None, folder="2025/03"):
    stem, ext, role, hint = photos.parse_name(name)
    return photos.DiskFile(Path(f"/lib/{folder}/{name}"), stem, ext, role, hint, taken, size=size)


def _match(assets, files):
    disk = {}
    for f in files:
        disk.setdefault(f.stem, []).append(f)
    return {a.uuid: (f.path.name, role, how) for a, f, role, how in photos.match_files(assets, disk)}


def test_photo_match_ext_alias_and_heic_jpg_guard():
    s = dt.timedelta(seconds=1)
    got = _match(
        [_A("jpeg", "IMG_0001.JPEG", 100), _A("near", "IMG_0002.HEIC", 100),
         _A("far", "IMG_0003.HEIC", 100), _A("noxmp", "IMG_0004.HEIC", 100), _A("pic", "IMG_0005.JPG", 100)],
        [_F("IMG_0001.jpg", 100),                       # jpeg≡jpg, XMP'siz (klasör ayı yeter)
         _F("IMG_0002.JPG", 100, _T0 + s),              # HEIC↔JPG ama XMP zamanı 1 sn → kabul
         _F("IMG_0003.JPG", 100, _T0 + 10 * s),         # 10 sn → başka foto, reddedilir
         _F("IMG_0004.JPG", 100),                       # XMP yok → farklı uzantıya güvenilmez
         _F("IMG_0005.MOV", 100, _T0)])                 # foto asset'i videoya bağlanmaz
    assert got == {"jpeg": ("IMG_0001.jpg", "original", "ad"), "near": ("IMG_0002.JPG", "original", "ad")}


def test_photo_match_renamed_file_by_time_and_size():
    s = dt.timedelta(seconds=1)
    got = _match(
        [_A("ren", "aaaa1111-bbbb.JPG", 5000), _A("big", "aaaa-bbbb.JPG", 5000, _T0 + 60 * s),
         _A("vid", "cccc.MOV", 9000, _T0 + 120 * s, video=True)],
        [_F("IMG_7000.JPG", 5100, _T0 + s),             # ad farklı; ±2 sn, ±4 KB → bağlanır
         _F("IMG_7001.JPG", 50000, _T0 + 60 * s),       # boyut çok farklı → bağlanmaz
         _F("IMG_7002.JPG", 9000, _T0 + 120 * s)])      # sınıf farklı (video ↔ foto) → bağlanmaz
    assert got == {"ren": ("IMG_7000.JPG", "original", "zaman+boyut")}


def test_photo_match_same_name_size_decides_and_order_independent():
    import random
    s = dt.timedelta(seconds=1)
    assets = [_A("small", "IMG_0100.JPG", 1000), _A("large", "IMG_0100.JPG", 9000),
              _A("later", "IMG_0100.JPG", 3000, _T0 + 3 * 3600 * s)]
    files = [_F("IMG_0100.JPG", 9000, _T0), _F("IMG_0100-1000.JPG", 1000, _T0),
             _F("IMG_0100-3000.JPG", 3000, _T0 + 3 * 3600 * s)]
    want = {"small": ("IMG_0100-1000.JPG", "original", "ad"), "large": ("IMG_0100.JPG", "original", "ad"),
            "later": ("IMG_0100-3000.JPG", "original", "ad")}
    rnd = random.Random(7)
    for _ in range(20):  # girdi sırası sonucu değiştirmez
        a, f = assets[:], [photos.DiskFile(**{**x.__dict__, "used": False}) for x in files]
        rnd.shuffle(a)
        rnd.shuffle(f)
        assert _match(a, f) == want


def test_photo_match_exif_copy_loses_to_plain_name():
    # '-<boyut>' son ekli dosya çoğu zaman --set-exif-datetime kopyası: eşit puanda soneksiz asıl kazanır
    got = _match([_A("one", "IMG_0200.JPG", 5000)],
                 [_F("IMG_0200.JPG", 5200, _T0), _F("IMG_0200-5000.JPG", 5000, _T0)])
    assert got == {"one": ("IMG_0200.JPG", "original", "ad")}


def test_photos_icloud_only_hidden_old_dated_and_snapshot_guard(conn, tmp_path):
    t1 = dt.datetime(2025, 1, 5, 10, 0, tzinfo=dt.timezone.utc)
    t_far = dt.datetime(2031, 1, 1, tzinfo=dt.timezone.utc)      # yanlış (geleceğe) tarihli asset
    t_old = dt.datetime(2019, 6, 1, 9, 0, tzinfo=dt.timezone.utc)  # snapshot'tan sonra eklenmiş eski foto
    raw_iphone = tmp_path / "data" / "raw" / "iphone"
    old = _photos_db(raw_iphone / "photos-db-2025-01-10" / "Photos.sqlite",
                     [(1, "A-1", 0, t1, "IMG_0001.JPG", 1, 0), (2, "A-2", 0, t_far, "IMG_0002.JPG", 1, 0)])
    lib = tmp_path / "srv" / "icloud-sync"
    _media_file(lib, "2025/01/IMG_0001.JPG", t1)
    _media_file(lib, "2019/06/scan-uuid.JPG", t_old)               # DB'de yok, kesimden önce tarihli
    _media_file(lib, "2019/06/hid-uuid.JPG", t_old + dt.timedelta(hours=1), rating=-1)
    _media_file(lib, "2019/06/noxmp.MOV", None)                    # XMP yok → olay değil, yalnız sayılır
    stats = photos.ingest(conn, library=lib)
    assert stats["icloud_only"] == 2 and stats["disk_unused"] == 1
    tags = {}
    for ext_id, tag in conn.execute("SELECT e.external_id, t.tag FROM event e JOIN tag t ON t.event_id=e.id "
                                    "WHERE e.external_id LIKE 'file:%'"):
        tags.setdefault(ext_id, set()).add(tag)
    assert tags == {"file:2019/06/scan-uuid.JPG": {"icloud-only"},
                    "file:2019/06/hid-uuid.JPG": {"icloud-only", "hidden"}}

    # yeni snapshot gelince eskisiyle ingest reddedilir (yoksa yeni snapshot olaylarını silerdi)
    _photos_db(raw_iphone / "photos-db-2025-02-05" / "Photos.sqlite", [(1, "A-1", 0, t1, "IMG_0001.JPG", 1, 0)])
    with pytest.raises(ValueError):
        photos.ingest(conn, db_path=old, library=lib)
    photos.ingest(conn, library=lib)
    assert conn.execute("SELECT count(*) FROM event WHERE external_id='A-2'").fetchone()[0] == 0


def test_photos_deleted_and_screenshot_flags(conn, tmp_path):
    import sqlite3
    t1 = dt.datetime(2025, 1, 5, 10, 0, tzinfo=dt.timezone.utc)
    t2 = t1 + dt.timedelta(hours=1)
    raw_iphone = tmp_path / "data" / "raw" / "iphone"
    p = _photos_db(raw_iphone / "photos-db-2025-01-10" / "Photos.sqlite",
                   [(1, "A-1", 0, t1, "IMG_0001.PNG", 1, 0), (2, "A-2", 0, t2, "IMG_0002.JPG", 1, 0),
                    (3, "A-3", 0, t1 + dt.timedelta(hours=2), "IMG_0003.PNG", 1, 0)])
    c = sqlite3.connect(p)
    c.execute("ALTER TABLE ZASSET ADD COLUMN ZISDETECTEDSCREENSHOT")
    c.execute("UPDATE ZASSET SET ZISDETECTEDSCREENSHOT=1 WHERE Z_PK=1")             # WhatsApp ile kaydedilmiş ss
    c.execute("UPDATE ZASSET SET ZKINDSUBTYPE=10, ZISDETECTEDSCREENSHOT=1 WHERE Z_PK=3")  # klasik ss
    c.execute("UPDATE ZASSET SET ZTRASHEDSTATE=1 WHERE Z_PK=2")                     # çöpte
    c.commit()
    c.close()
    lib = tmp_path / "srv" / "icloud-sync"
    _media_file(lib, "2025/01/IMG_0002.JPG", t2)
    stats = photos.ingest(conn, library=lib)
    tags = {}
    for ext_id, tag in conn.execute("SELECT e.external_id, t.tag FROM event e JOIN tag t ON t.event_id=e.id"):
        tags.setdefault(ext_id, set()).add(tag)
    assert tags["A-1"] == {"screenshot"} and tags["A-3"] == {"screenshot"}
    assert tags["A-2"] == {"deleted"}
    # çöpteki asset'in arşiv dosyası ona bağlanır, icloud-only olarak geri gelmez
    assert stats.get("icloud_only", 0) == 0 and stats["deleted"] == 1
    assert conn.execute("SELECT e.external_id FROM media m JOIN event e ON e.id=m.event_id").fetchall() == [("A-2",)]
