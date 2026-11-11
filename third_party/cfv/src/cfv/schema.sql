-- CFV ortak şema — v1 (2026-11-01)
-- Tek zaman çizelgesi: her kaynaktaki her kayıt bir `event`.
-- Kaynağa özgü ayrıntı `payload` (JSON) içinde; ortak alanlar kolon.
-- Bu DB data/raw'dan HER ZAMAN yeniden üretilebilir olmalı (raw = gerçek kaynak).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Bir içe aktarma kaynağı = belirli bir ham dosya/anlık görüntü
-- (örn. "iphone-photos-db" @ data/raw/iphone/photos-db-2026-11-01/Photos.sqlite).
CREATE TABLE IF NOT EXISTS source (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,            -- 'iphone-photos', 'apple-notes', 'apple-music-activity', 'calendar', 'bereal', 'snapchat', 'youtube', ...
    raw_path    TEXT NOT NULL,            -- data/ köküne göre göreli yol
    snapshot    TEXT,                     -- export/yedek tarihi (ISO)
    imported_at TEXT NOT NULL,            -- UTC ISO-8601
    importer    TEXT NOT NULL,            -- 'cfv.ingest.photos@0.0.1'
    UNIQUE (kind, raw_path)
);

-- Zaman çizelgesinin atomu.
CREATE TABLE IF NOT EXISTS event (
    id           INTEGER PRIMARY KEY,
    source_id    INTEGER NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    external_id  TEXT NOT NULL,           -- kaynaktaki kararlı kimlik (asset UUID, note id, ...): idempotent import
    kind         TEXT NOT NULL,           -- 'photo','video','play','calendar','note','bereal','snap','youtube_watch', ...
    start_utc    TEXT NOT NULL,           -- ISO-8601 UTC ('2024-07-14T18:03:22Z')
    end_utc      TEXT,                    -- süre varsa (takvim, dinleme)
    tz_offset_min INTEGER,                -- çekildiği/yaşandığı yerel saat farkı (biliniyorsa)
    local_date   TEXT NOT NULL,           -- yerel gün 'YYYY-MM-DD' (klasörleme/gruplama anahtarı)
    time_quality TEXT NOT NULL DEFAULT 'exact', -- 'exact' | 'day' | 'week' | 'approx'
    title        TEXT,
    body         TEXT,                    -- not metni, takvim açıklaması vb. (FTS'e girer)
    lat          REAL,
    lon          REAL,
    place_id     INTEGER REFERENCES place(id),
    payload      TEXT,                    -- JSON: kaynağa özgü alanlar
    UNIQUE (source_id, external_id)
);
CREATE INDEX IF NOT EXISTS event_time ON event (start_utc);
CREATE INDEX IF NOT EXISTS event_kind_time ON event (kind, start_utc);
CREATE INDEX IF NOT EXISTS event_day ON event (local_date);

-- Diskteki dosyalar (fotoğraf/video kendisi KOPYALANMAZ; yol + hash tutulur).
CREATE TABLE IF NOT EXISTS media (
    id                INTEGER PRIMARY KEY,
    event_id          INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    path              TEXT NOT NULL,      -- mutlak yol (örn. ~/Pictures/icloud-sync/2024/07/IMG_1234.HEIC)
    role              TEXT NOT NULL DEFAULT 'original', -- 'original' | 'adjusted' | 'live' | 'thumb'
    original_filename TEXT,
    mime              TEXT,
    width             INTEGER,
    height            INTEGER,
    duration_s        REAL,
    blake2b           TEXT,
    UNIQUE (path)
);

-- Kişiler: kaynaklar arası tek kimlik. Aynı insan Photos'ta "Ayşe Y.", Snapchat'te "ayse99" olabilir.
CREATE TABLE IF NOT EXISTS person (
    id           INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    is_me        INTEGER NOT NULL DEFAULT 0,
    notes        TEXT
);
CREATE TABLE IF NOT EXISTS person_alias (
    person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    source_kind TEXT NOT NULL,            -- 'iphone-photos', 'snapchat', 'contacts', ...
    alias     TEXT NOT NULL,              -- kaynaktaki kimlik (ZPERSON.ZPERSONUUID, WhatsApp JID ...)
    name      TEXT,                       -- kaynaktaki görünen ad
    PRIMARY KEY (source_kind, alias)
);

-- Olay ↔ kişi (fotoda yüz, takvim katılımcısı, snap karşı tarafı, notta bahsedilen ...).
CREATE TABLE IF NOT EXISTS event_person (
    event_id   INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    person_id  INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,             -- 'in_photo' | 'attendee' | 'counterpart' | 'mentioned'
    confidence REAL,                      -- 1.0 = elle etiket; otomatik eşleşmede < 1
    PRIMARY KEY (event_id, person_id, role)
);

CREATE TABLE IF NOT EXISTS place (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    lat     REAL,
    lon     REAL,
    payload TEXT
);

-- Müzik: aynı parça binlerce kez dinlenir → parça ayrı, her dinleme bir 'play' event'i.
CREATE TABLE IF NOT EXISTS track (
    id       INTEGER PRIMARY KEY,
    title    TEXT NOT NULL,
    artist   TEXT,
    album    TEXT,
    apple_id TEXT,
    isrc     TEXT,
    UNIQUE (title, artist, album)
);
CREATE TABLE IF NOT EXISTS event_track (
    event_id INTEGER PRIMARY KEY REFERENCES event(id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL REFERENCES track(id),
    played_ms INTEGER                    -- ne kadar dinlendi (skip ayırt etmek için)
);

-- Serbest etiketler / koleksiyonlar (albüm, "IG adayı", BeReal geç/zamanında ...).
CREATE TABLE IF NOT EXISTS tag (
    event_id INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    tag      TEXT NOT NULL,
    PRIMARY KEY (event_id, tag)
);

-- Tam metin arama (notlar, takvim başlıkları, video başlıkları ...).
-- unicode61 Türkçe ı/İ'yi i'ye katlamaz → triggerlar metni katlayarak yazar;
-- sorgular da cfv.db.fold_tr() ile katlanmalı. Contentless: gösterim için event'e JOIN.
-- WhatsApp'ta title = sohbet adı; her mesajı sohbet adıyla eşleştirmesin diye indekslenmez.
CREATE VIRTUAL TABLE IF NOT EXISTS event_fts USING fts5(
    title, body, content='', contentless_delete=1,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS event_ai AFTER INSERT ON event BEGIN
    INSERT INTO event_fts(rowid, title, body) VALUES (
        new.id,
        CASE WHEN new.kind = 'whatsapp' THEN NULL
             ELSE replace(replace(new.title, 'ı', 'i'), 'İ', 'i') END,
        replace(replace(new.body,  'ı', 'i'), 'İ', 'i'));
END;
CREATE TRIGGER IF NOT EXISTS event_ad AFTER DELETE ON event BEGIN
    DELETE FROM event_fts WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS event_au AFTER UPDATE OF title, body ON event BEGIN
    DELETE FROM event_fts WHERE rowid = old.id;
    INSERT INTO event_fts(rowid, title, body) VALUES (
        new.id,
        CASE WHEN new.kind = 'whatsapp' THEN NULL
             ELSE replace(replace(new.title, 'ı', 'i'), 'İ', 'i') END,
        replace(replace(new.body,  'ı', 'i'), 'İ', 'i'));
END;

CREATE INDEX IF NOT EXISTS event_person_person ON event_person (person_id);
CREATE INDEX IF NOT EXISTS media_event ON media (event_id);

-- Olaylar arası türetilmiş bağlar (ör. foto 'photo_at' takvim olayı). cfv.link yeniden üretir.
CREATE TABLE IF NOT EXISTS event_link (
    a    INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    b    INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    PRIMARY KEY (a, b, kind)
);
CREATE INDEX IF NOT EXISTS event_link_b ON event_link (b, kind);

-- Rüya günlüğü (cfv.ruya, sources/dream.py). Rüya METNİ yalnız `dream` tablosunda durur: event.body boş,
-- böylece FTS, ağaç, site, raporlar metni hiç görmez. Kişi önerileri event_person(role='dream').
CREATE TABLE IF NOT EXISTS dream (
    xid       TEXT PRIMARY KEY,          -- 'ruya-YYYY-MM-DD' (gecenin tarihi)
    event_id  INTEGER REFERENCES event(id) ON DELETE CASCADE,
    local_date TEXT NOT NULL,
    recalled  INTEGER NOT NULL,          -- 0 = "hatırlamıyorum"
    text      TEXT,
    parts     TEXT NOT NULL              -- JSON [{id, src, at, kind, len}]
);
CREATE TABLE IF NOT EXISTS dream_eval (
    xid           TEXT PRIMARY KEY,      -- dream.xid
    job           TEXT NOT NULL,         -- data/processed/ruya/is/<job>.json
    model         TEXT,
    context_until TEXT NOT NULL,         -- yalnız bu andan önceki olaylar kullanıldı (UTC)
    context_partial INTEGER NOT NULL DEFAULT 0,
    emotion       TEXT, valence INTEGER, intensity INTEGER,
    themes        TEXT, places TEXT, persons TEXT, day_links TEXT, signals TEXT,  -- JSON
    reasoning     TEXT,
    raw_json      TEXT NOT NULL
);

INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '2');
