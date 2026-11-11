"""Rehber okuyucusu testleri — tamamen SENTETİK veri (+90 000 111 … kalıbı, RFC 2606 example.com). Repo PUBLIC."""

import sqlite3

import pytest

from cfv import db
from cfv.ingest import Batch, iphone_extract
from cfv.sources import contacts


@pytest.mark.parametrize("raw,expected", [
    ("+900001112233", "+900001112233"),
    ("+90 000 111 22 33", "+900001112233"),
    ("+90 (000) 111 22 33", "+900001112233"),
    ("0 (000) 111 22 33", "+900001112233"),   # 0 + 10 hane; '00' öneki sanılmamalı
    ("00001112233", "+900001112233"),
    ("900001112233", "+900001112233"),
    ("00 90 000 111 22 33", "+900001112233"),
    ("(200) 000 00 00", "+902000000000"),     # 10 hane, TR alan kodu biçimi (atanmamış 200)
    ("(000) 111 22 33", None),                # 10 hane ama 0'la başlıyor: alan kodu değil
    ("0049 000 000000", "+49000000000"),
    ("+1 (200) 555-0123", "+12005550123"),
    ("+0001112233", None),                    # ülke kodu 0'la başlamaz
    ("0000000", None),                        # kısa/yerel numara: emin değiliz
    ("Array", None),
    ("", None),
    (None, None),
    (b"\x00", None),
])
def test_e164(raw, expected):
    assert contacts.e164(raw) == expected


def make_abook(path, people, values, labels=("_$!<Mobile>!$_", "old number", "İş")):
    """people: (rowid, guid, first, last, org); values: (record_id, property, label_rowid|None, value)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE ABPerson (ROWID INTEGER PRIMARY KEY, First TEXT, Last TEXT, Middle TEXT,
                               Organization TEXT, Nickname TEXT, Kind INTEGER DEFAULT 0, guid TEXT);
        CREATE TABLE ABMultiValue (UID INTEGER PRIMARY KEY, record_id INTEGER, property INTEGER,
                                   identifier INTEGER, label INTEGER, value TEXT, guid TEXT);
        CREATE TABLE ABMultiValueLabel (value TEXT);""")
    c.executemany("INSERT INTO ABMultiValueLabel (value) VALUES (?)", [(x,) for x in labels])
    c.executemany("INSERT INTO ABPerson (ROWID, guid, First, Last, Organization) VALUES (?,?,?,?,?)", people)
    c.executemany("INSERT INTO ABMultiValue (record_id, property, identifier, label, value) VALUES (?,?,?,?,?)",
                  [(r, p, i, lab, v) for i, (r, p, lab, v) in enumerate(values)])
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


def seed_existing(conn):
    """Önceden içe aktarılmış WhatsApp ve Gmail kişileri gibi."""
    b = Batch(conn, "whatsapp", conn_path := db.data_dir() / "raw" / "wa.sqlite", "test")
    wa = b.person("900001112201@s.whatsapp.net", "Ayşe WA")
    wa_conflict = b.person("900001112204@s.whatsapp.net", "Can WA")
    g = Batch(conn, "gmail", conn_path, "test")
    mail = g.person("mehmet@example.com", "Mehmet G", source_kind="email")
    mail_conflict = g.person("can@example.com", "Can G", source_kind="email")
    return wa, wa_conflict, mail, mail_conflict


PEOPLE = [
    (1, "G-AYSE", "Ayşe", "Örnek", None),
    (2, "G-MEHMET", "Mehmet", None, None),
    (3, "G-EV1", "Ev", "Bir", None),
    (4, "G-EV2", "Ev", "İki", None),
    (5, "G-CAN", "Can", None, None),
    (6, "G-FIRMA", None, None, "Örnek Ltd"),
    (7, "G-YENI", "Yeni", "Kişi", None),
]
VALUES = [
    (1, 3, 1, "0 (000) 111 22 01"),       # WhatsApp kişisiyle eşleşir
    (1, 3, 2, "+900001112299"),           # 'old number' → atlanır
    (2, 4, None, " Mehmet@Example.com "),   # Gmail kişisiyle eşleşir (küçük harf)
    (3, 3, None, "0 (000) 111 22 09"),    # iki kayıtta ortak ev telefonu → belirsiz
    (4, 3, None, "+90 000 111 22 09"),
    (5, 3, 1, "00001112204"),             # numara WA'da bir kişide, e-posta Gmail'de başka kişide
    (5, 4, None, "can@example.com"),
    (6, 3, 3, "0000000"),                 # kısa numara → çözümlenemedi
    (7, 3, 1, "+90 000 111 22 07"),
]


def test_contacts_aliases_matching_and_idempotent(conn, tmp_path):
    root = tmp_path / "data" / "raw" / "iphone"
    # .tamam'lı yeni çıkarım seçilmeli; işaretsiz daha yeni klasör (yarım kopya) atlanmalı
    make_abook(root / "extract-2026-11-15" / "HomeDomain/Library/AddressBook/AddressBook.sqlitedb", PEOPLE, VALUES)
    (root / "extract-2026-11-15" / ".tamam").touch()
    make_abook(root / "extract-2026-11-22" / "HomeDomain/Library/AddressBook/AddressBook.sqlitedb", [], [])
    assert iphone_extract().name == "extract-2026-11-15"

    wa, wa_conflict, mail, mail_conflict = seed_existing(conn)
    persons_before = conn.execute("SELECT count(*) FROM person").fetchone()[0]

    stats = contacts.ingest(conn)
    assert stats["contacts"] == 7
    assert stats["matched_existing"] == 3          # Ayşe→WA, Mehmet→Gmail, Can→WA (çakışmalı)
    assert stats["conflicts"] == 1                 # Can: WA ve Gmail ayrı kişiler, birleştirilmez
    assert stats["shared_numbers"] == 1 and stats["old_numbers"] == 1 and stats["unparsed_numbers"] == 1
    assert stats["new_persons"] == 4               # Ev Bir, Ev İki, Örnek Ltd, Yeni Kişi

    assert alias_owner(conn, "contacts", "G-AYSE") == wa
    assert alias_owner(conn, "phone", "+900001112201") == wa
    assert alias_owner(conn, "phone", "+900001112299") is None
    assert alias_owner(conn, "contacts", "G-MEHMET") == mail
    assert alias_owner(conn, "contacts", "G-CAN") == wa_conflict
    assert alias_owner(conn, "email", "can@example.com") == mail_conflict   # dokunulmadı
    assert alias_owner(conn, "phone", "+900001112209") is None            # ortak hat
    yeni = alias_owner(conn, "contacts", "G-YENI")
    assert alias_owner(conn, "whatsapp", "900001112207@s.whatsapp.net") == yeni
    assert conn.execute("SELECT display_name FROM person WHERE id=?", (yeni,)).fetchone()[0] == "Yeni Kişi"
    assert conn.execute("SELECT display_name FROM person WHERE id=?", (wa,)).fetchone()[0] == "Ayşe WA"
    assert conn.execute("SELECT display_name FROM person WHERE id=?",
                        (alias_owner(conn, "contacts", "G-FIRMA"),)).fetchone()[0] == "Örnek Ltd"
    assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 0
    assert conn.execute("SELECT snapshot FROM source WHERE kind='contacts'").fetchone()[0] == "2026-11-15"

    # tekrar koşmak hiçbir şeyi değiştirmez
    aliases = conn.execute("SELECT * FROM person_alias ORDER BY 1, 2, 3").fetchall()
    again = contacts.ingest(conn)
    assert again["new_persons"] == 0 and again["matched_existing"] == 0
    assert conn.execute("SELECT * FROM person_alias ORDER BY 1, 2, 3").fetchall() == aliases
    assert conn.execute("SELECT count(*) FROM person").fetchone()[0] == persons_before + 4


def test_contacts_then_whatsapp_reuses_person(conn, tmp_path):
    """Boş DB'de rehber önce koşarsa WhatsApp okuyucusu aynı kişiyi bulur (Batch.person alias ile)."""
    path = tmp_path / "data" / "raw" / "iphone" / "extract-2026-11-15" / "HomeDomain/Library/AddressBook/AddressBook.sqlitedb"
    make_abook(path, [(1, "G-A", "Ayşe", None, None)], [(1, 3, 1, "0 (000) 111 22 01")])
    contacts.ingest(conn, path)
    pid = alias_owner(conn, "contacts", "G-A")
    b = Batch(conn, "whatsapp", path, "test")
    assert b.person("900001112201@s.whatsapp.net", "Ayşe WA") == pid
