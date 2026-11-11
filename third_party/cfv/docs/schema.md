# Ortak şema — v2

Kaynak: [`src/cfv/schema.sql`](../src/cfv/schema.sql). Bu belge *neden*i ve kaynak
eşlemesini anlatır.

## Temel fikir

Her şey bir **`event`**: zamanı olan tek bir kayıt (bir foto, bir dinleme, bir takvim
etkinliği, bir not, bir BeReal...). Ortak sorular ("şu tarih aralığında ne oldu?",
"kim vardı?", "nerede?") ortak kolonlarla yanıtlanır; kaynağa özgü her şey `payload`
JSON'unda durur — şemayı her yeni kaynakta değiştirmek gerekmez.

```
source 1─* event *─* person        (event_person: in_photo / attendee / counterpart / mentioned)
             │  ├─* media          (diskteki dosya; kopyalanmaz)
             │  ├─1 event_track ─ track   (müzik dinlemeleri)
             │  ├─* tag
             │  └─? place
             └─ event_fts (FTS5: title + body)
person 1─* person_alias            (kaynaklar arası aynı insan)
```

## Tablolar

| Tablo | Amaç |
|-------|------|
| `source` | Bir import = belirli bir ham dosya/anlık görüntü. Yeniden import aynı satırı kullanır. |
| `event` | Zaman çizelgesi atomu. `(source_id, external_id)` benzersiz → idempotent import. |
| `media` | Foto/video dosya yolu, rol (original/adjusted/live), boyut, hash. |
| `person`, `person_alias` | Tek kişi kimliği; Photos ZPERSON, Contacts, Snapchat kullanıcı adı vb. alias olur. |
| `event_person` | Olay ↔ kişi; `confidence` < 1 = otomatik eşleşme. |
| `track`, `event_track` | Parça bir kez; her dinleme bir `play` event'i + `played_ms`. |
| `place` | Adlandırılmış yer (ileride ters geocoding/kümeleme). |
| `tag` | Serbest etiket: albüm adı, "ig-aday", BeReal "geç" vb. |
| `event_fts` | Türkçe-dostu tam metin (unicode61, remove_diacritics). |

## Zaman kuralları

- `start_utc`/`end_utc`: ISO-8601 UTC, `Z` sonekli. Lexicographic sıralama = zaman sıralaması.
- `tz_offset_min`: olayın yerel saat farkı (fotoğraf EXIF'i, takvim tz'si). "O gün" sorguları
  yerel güne göre yapılmalı.
- `time_quality`: `exact` | `day` (tüm gün / günlük özet) | `week` (günü belirsiz ödev) | `approx`.
- `local_date`: olayın yerel günü; klasörleme ve tarih süzgeçleri bunu kullanır.
- Apple Core Data zamanı: `unix = coredata + 978307200`.

## Kaynak → event eşlemesi (taslak)

| Kaynak | `kind` | `external_id` | Ortak alanlar | `payload` örnekleri |
|--------|--------|---------------|---------------|---------------------|
| Photos.sqlite ZASSET | `photo` / `video` | `ZUUID` | ZDATECREATED, ZLATITUDE/ZLONGITUDE, kişiler ← ZDETECTEDFACE→ZPERSON (birleştirmeler çözülür) | moment (yer), albümler; etiket: favorite/hidden/screenshot/live |
| icloud-sync dosyası | (photo event'ine `media`) | — | yol, hash | XMP alanları |
| NoteStore | `note` | note `ZIDENTIFIER` | oluşturma zamanı, title, body (markdown) | klasör, değiştirilme zamanı |
| NoteStore "Ödevler YYYY" | `todo` | hash(not, hafta, gün, metin, sıra) | hafta+gün → `local_date` (`time_quality` day/week), title = madde | done, week, weekday, section |
| Calendar.sqlitedb | `calendar` | etkinlik UUID | başlangıç/bitiş, title, konum | takvim adı, tekrar kuralı |
| Apple Music Play Activity | `play` | (zaman+parça hash'i) | zaman, süre → `event_track` | end reason (skip/tam), cihaz |
| WhatsApp ChatStorage | `whatsapp` | sohbet JID / stanza id | zaman, title = sohbet adı (FTS'e girmez), body = metin | sender, from_me, chat_type, type |
| Zepp | `zepp_day` / `sleep` / `workout` | `day/…`, `sleep/…`, `workout/…` | gün / uyku aralığı / egzersiz aralığı | adım, PAI, nabız; uyku evreleri; spor türü |
| Snapchat | `snap` / `chat` / `memory` | export kimliği | zaman, karşı taraf → `event_person` | medya türü |
| BeReal | `bereal` | post id | zaman, konum, medya ×2 | geç mi, retake sayısı |
| YouTube | `youtube_watch` | (zaman+url hash'i) | zaman, title | kanal, url |

## Açık sorular

1. Kişi birleştirme (Photos adı ↔ Contacts ↔ Snapchat) ne kadar otomatik olsun? Öneri:
   otomatik öneri üret, onayı Tuna versin (`confidence`).
2. Silinmiş/gizli fotolar (`ZTRASHEDSTATE`, `ZHIDDEN`) import edilsin mi? Öneri: edilsin,
   `payload`'da işaretli; MCP varsayılan olarak gizlileri göstermesin.
3. Not gövdesi (protobuf) tam metin mi, düz metin mi? Öneri: düz metin + ek listesi.
