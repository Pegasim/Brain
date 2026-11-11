"""Şema smoke testi — tamamen SENTETİK veri."""

from cfv import db


def test_schema_roundtrip(tmp_path):
    path = db.init_db(tmp_path / "t.sqlite")
    db.init_db(path)  # idempotent olmalı
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO source (kind, raw_path, imported_at, importer) VALUES "
        "('test', 'raw/test.json', '2026-01-01T00:00:00Z', 'test')"
    )
    conn.execute(
        "INSERT INTO event (source_id, external_id, kind, start_utc, local_date, title, body) VALUES "
        "(1, 'e1', 'note', '2025-06-01T12:00:00Z', '2025-06-01', 'Örnek not', 'deniz kenarında kahvaltı')"
    )
    conn.execute("INSERT INTO person (display_name) VALUES ('Örnek Kişi')")
    conn.execute("INSERT INTO event_person VALUES (1, 1, 'mentioned', 1.0)")
    def search(q):
        return conn.execute(
            "SELECT rowid FROM event_fts WHERE event_fts MATCH ?", (db.fold_tr(q),)
        ).fetchall()

    # Türkçe katlama: ı/İ/ş/ğ/ö/ü/ç farkı aramayı bozmamalı
    assert search("kahvaltı") == [(1,)]
    assert search("KAHVALTI") == [(1,)]
    assert search("denız kenarinda") == [(1,)]
    assert search("ornek") == [(1,)]

    conn.execute("UPDATE event SET body = 'akşam yemeği' WHERE id = 1")
    assert search("kahvaltı") == []
    assert search("aksam") == [(1,)]
    conn.execute("DELETE FROM event WHERE id = 1")
    assert search("aksam") == []
