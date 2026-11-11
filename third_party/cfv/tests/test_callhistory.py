"""Arama geçmişi okuyucusu testleri — tamamen SENTETİK veri (+90 000 111 … kalıbı, example.com). Repo PUBLIC."""

import datetime as dt
import json
import sqlite3

import pytest

from cfv import db
from cfv.ingest import Batch
from cfv.sources import callhistory
from cfv.timeutil import COREDATA_EPOCH

UTC = dt.timezone.utc
STORE = "HomeDomain/Library/CallHistoryDB/CallHistory.storedata"


def at(h, m=0, s=0):
    return dt.datetime(2026, 11, 3, h, m, s, tzinfo=UTC)


def cd(t):
    return t.timestamp() - COREDATA_EPOCH


def make_store(path, rows):
    """rows: (uid, t, duration, originated, answered, category, handle_type, address, name, provider)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE ZCALLRECORD (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
                 ZANSWERED INTEGER, ZCALL_CATEGORY INTEGER, ZCALLTYPE INTEGER, ZDISCONNECTED_CAUSE INTEGER,
                 ZFACE_TIME_DATA BLOB, ZHANDLE_TYPE INTEGER, ZNUMBER_AVAILABILITY INTEGER, ZORIGINATED INTEGER,
                 ZREAD INTEGER, ZDATE TIMESTAMP, ZDURATION FLOAT, ZADDRESS VARCHAR, ZDEVICE_ID VARCHAR,
                 ZISO_COUNTRY_CODE VARCHAR, ZLOCATION VARCHAR, ZNAME VARCHAR, ZSERVICE_PROVIDER VARCHAR,
                 ZUNIQUE_ID VARCHAR)""")
    c.executemany("""INSERT INTO ZCALLRECORD (ZUNIQUE_ID, ZDATE, ZDURATION, ZORIGINATED, ZANSWERED, ZCALL_CATEGORY,
                     ZHANDLE_TYPE, ZADDRESS, ZNAME, ZSERVICE_PROVIDER, ZISO_COUNTRY_CODE)
                     VALUES (?,?,?,?,?,?,?,?,?,?,'tr')""",
                  [(u, cd(t), d, o, a, cat, ht, addr, n, p) for u, t, d, o, a, cat, ht, addr, n, p in rows])
    c.commit()
    c.close()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CFV_DATA", str(tmp_path / "data"))
    return db.connect(db.init_db(tmp_path / "t.sqlite"))


def alias_owner(conn, kind, alias):
    row = conn.execute("SELECT person_id FROM person_alias WHERE source_kind=? AND alias=?",
                       (kind, alias)).fetchone()
    return row[0] if row else None


def seed(conn, tmp_path):
    """Rehberden gelmiş kişi A (telefon + WA takma adı), WA'da kişi B; iki WhatsApp arama olayı."""
    a = conn.execute("INSERT INTO person (display_name) VALUES ('Ayşe')").lastrowid
    conn.execute("INSERT INTO person_alias VALUES (?, 'phone', '+900001112201', 'Ayşe')", (a,))
    conn.execute("INSERT INTO person_alias VALUES (?, 'whatsapp', '900001112201@s.whatsapp.net', 'Ayşe')", (a,))
    wa = Batch(conn, "whatsapp", tmp_path / "data" / "raw" / "wa.sqlite", "test")
    b = wa.person("900001112202@s.whatsapp.net", "Bora")
    e1 = wa.event("x/1", "whatsapp", at(10, 0, 0), payload={"type": "call", "chat_type": "dm"})
    wa.link_person(e1, a, "counterpart")
    e2 = wa.event("x/2", "whatsapp", at(12, 0, 0), payload={"type": "call", "chat_type": "dm"})
    wa.link_person(e2, b, "counterpart")
    wa.event("x/3", "whatsapp", at(14, 0, 0), payload={"type": "text"})  # arama değil
    return a, b


WA, TEL, FT = "net.whatsapp.WhatsAppSMB", "com.apple.Telephony", "com.apple.FaceTime"
ROWS = [
    ("U1", at(9), 120, 1, 1, 1, 2, "0 (000) 111 22 01", None, TEL),        # giden, rehber kişisi A
    ("U2", at(10, 0, 30), 300, 0, 1, 1, 2, "+900001112201", None, WA),    # WA çifti (kişi + ±60 sn)
    ("U3", at(12, 0, 20), 60, 1, 1, 2, 1, "OPAK-0001", None, WA),         # WA çifti (opak, pencerede tek arama)
    ("U4", at(13), 45, 0, 1, 1, 2, "+900001112205", None, WA),            # WA, eşi yok → arama olayı, yeni kişi
    ("U5", at(15), 0, 0, 0, 1, 2, "+90 000 111 22 07", "Kargo", TEL),     # cevapsız, yeni kişi
    ("U6", at(16), 600, 0, 1, 2, 3, "Kisi@Example.com", None, FT),        # FaceTime görüntülü, e-posta
    ("U7", at(17), 30, 0, 1, 1, 2, None, None, TEL),                     # gizli numara
    ("U8", at(12, 0, 40), 90, 0, 1, 1, 2, "+900001112201", None, WA),     # pencerede B'nin araması var, kişi A → çift DEĞİL
    ("U9", at(10, 2, 0), 30, 1, 1, 1, 2, "+900001112201", None, WA),      # A, ama 90 sn sonra → çift değil
]


def test_callhistory(conn, tmp_path):
    a, b = seed(conn, tmp_path)
    path = tmp_path / "data" / "raw" / "iphone" / "extract-2026-11-15" / STORE
    make_store(path, ROWS)
    persons_before = conn.execute("SELECT count(*) FROM person").fetchone()[0]

    stats = callhistory.ingest(conn, path)
    assert stats["whatsapp_duplicates"] == 2
    assert stats["whatsapp_unmatched"] == 3                     # U4, U8, U9
    assert stats["calls"] == 7
    assert stats["missed"] == 1 and stats["outgoing"] == 2 and stats["incoming"] == 4
    assert stats["unresolved_handles"] == 1
    assert stats["new_persons"] == 3                            # +…05, +…07, e-posta

    conn.row_factory = sqlite3.Row
    calls = {r["external_id"]: r for r in conn.execute(
        "SELECT e.*, (SELECT person_id FROM event_person WHERE event_id=e.id) pid FROM event e "
        "JOIN source s ON s.id=e.source_id WHERE s.kind='callhistory'")}
    assert set(calls) == {"U1", "U4", "U5", "U6", "U7", "U8", "U9"}
    assert calls["U1"]["pid"] == a and calls["U8"]["pid"] == a
    assert calls["U1"]["title"] == "giden arama: +900001112201"
    assert calls["U1"]["end_utc"] == "2026-11-03T09:02:00Z"
    assert calls["U5"]["title"] == "cevapsız arama: Kargo" and calls["U5"]["end_utc"] is None
    assert calls["U5"]["pid"] == alias_owner(conn, "phone", "+900001112207")
    assert calls["U6"]["pid"] == alias_owner(conn, "email", "kisi@example.com")
    assert calls["U7"]["pid"] is None

    p4 = json.loads(calls["U4"]["payload"])
    assert p4["provider"] == "whatsapp" and p4["number"] == "+900001112205"
    assert json.loads(calls["U6"]["payload"])["media"] == "video"
    assert conn.execute("SELECT count(*) FROM person").fetchone()[0] == persons_before + 3

    # tekrar koşmak aynı sonucu verir, yeni kişi açmaz
    again = callhistory.ingest(conn, path)
    assert again["calls"] == 7 and again["new_persons"] == 0
    assert conn.execute("SELECT count(*) FROM event e JOIN source s ON s.id=e.source_id "
                        "WHERE s.kind='callhistory'").fetchone()[0] == 7
    assert conn.execute("SELECT count(*) FROM person").fetchone()[0] == persons_before + 3


def test_wa_opaque_ambiguous_is_kept(conn, tmp_path):
    """Opak tanıtıcı + pencerede iki WhatsApp araması → hangisi belli değil, olay korunur."""
    wa = Batch(conn, "whatsapp", tmp_path / "wa.sqlite", "test")
    wa.event("x/1", "whatsapp", at(10, 0, 0), payload={"type": "call"})
    wa.event("x/2", "whatsapp", at(10, 0, 50), payload={"type": "call"})
    path = tmp_path / STORE
    make_store(path, [("U1", at(10, 0, 20), 60, 1, 1, 1, 1, "OPAK-0002", None, WA)])
    stats = callhistory.ingest(conn, path)
    assert stats["whatsapp_duplicates"] == 0 and stats["calls"] == 1


def test_union_of_snapshots(conn, tmp_path):
    """iOS eski aramayı silse de önceki görüntüdeki kayıt kalır; aynı kayıtta en yeni görüntü geçerli."""
    root = tmp_path / "data" / "raw" / "iphone"
    make_store(root / "extract-2026-11-08" / STORE, [
        ("OLD", at(8), 10, 1, 1, 1, 2, "+900001112201", None, TEL),
        ("SAME", at(9), 10, 1, 1, 1, 2, "+900001112201", None, TEL)])
    (root / "extract-2026-11-08" / ".tamam").touch()
    make_store(root / "extract-2026-11-15" / STORE, [
        ("SAME", at(9), 99, 1, 1, 1, 2, "+900001112201", None, TEL),
        ("NEW", at(11), 10, 1, 1, 1, 2, "+900001112201", None, TEL)])
    (root / "extract-2026-11-15" / ".tamam").touch()
    make_store(root / "extract-2026-11-22" / STORE, [   # .tamam yok: yarım kopya, okunmaz
        ("HALF", at(12), 10, 1, 1, 1, 2, "+900001112201", None, TEL)])

    stats = callhistory.ingest(conn)
    assert stats["snapshots"] == 2 and stats["calls"] == 3
    rows = dict(conn.execute("SELECT external_id, json_extract(payload, '$.duration_s') FROM event").fetchall())
    assert rows == {"OLD": 10, "SAME": 99, "NEW": 10}
    assert conn.execute("SELECT snapshot FROM source WHERE kind='callhistory'").fetchone()[0] == "2026-11-15"
