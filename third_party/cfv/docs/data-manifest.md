# Veri manifestosu

Her kaynak için: nerede durur, formatı, ne çıkarılacak, durumu. Yollar `data/` köküne
göredir (aksi belirtilmedikçe). Bu belge **kişisel içerik barındırmaz** — yalnızca
yapı ve sayımlar.

Durum anahtarı: ⬜ bekleniyor · 🟨 kısmi/sürüyor · 🟩 ham veri hazır · ✅ import edildi

| # | Kaynak | Yer | Format | Çıkacak olan | Durum |
|---|--------|-----|--------|--------------|-------|
| 1 | iPhone Photos kütüphanesi DB | `raw/iphone/photos-db-2026-11-01/Photos.sqlite` | SQLite (Core Data), 1.1 GB | asset zamanı/konumu, orijinal dosya adı, yüz → kişi, albüm, moment (yer adı) | ✅ `cfv ingest photos` — 28.596 event, 108 kişi |
| 2 | Foto/video dosyaları | `~/Pictures/icloud-sync/YYYY/AA/` (data/ dışında) | HEIC/JPG/MOV + XMP sidecar | `media` satırları (yol, rol) | ✅ 18.253 asset dosyasına bağlı (+adjusted/live/alt); 🟨 icloudpd hâlâ iniyor (2022/09 öncesi + videolar) |
| 3 | iPhone tam yedeği (şifresiz) | `raw/iphone/backup/<UDID>/` | idevicebackup2 (Manifest.db + hash'li dosyalar), 22 GB, 78.195 dosya | aşağıdaki 3a–3d (+ mesajlar, arama geçmişi, uygulama verileri) | 🟩 |
| 3a | ↳ iCloud Notlar | `raw/iphone/extract-2026-11-01/AppDomainGroup-group.com.apple.notes/NoteStore.sqlite` | SQLite; gövde `ZICNOTEDATA.ZDATA` gzip'li protobuf | 54 not (2018-12 → 2026-10); başlık, metin, zamanlar, klasör | ✅ 35 dolu not; 5 "Ödevler" notundan 4.884 `todo` (2023-W44 →) — sayı az görünüyor → privacy exportuyla karşılaştır |
| 3b | ↳ Takvim | `raw/iphone/extract-2026-11-01/HomeDomain/Library/Calendar/Calendar.sqlitedb` | SQLite | iCloud takvimleri, Found in Mail, anımsatıcılar (doğum günü + tatiller atlanır). **Ana takvim Google** → #8 | ✅ 91 event |
| 3c | ↳ Müzik kütüphanesi | `raw/iphone/afc-2026-11-01/iTunes_Control/iTunes/MediaLibrary.sqlitedb` | SQLite | 4.019 parça (2.614 çalınmış): `item_stats` çalma/skip sayısı, son çalma, beğeni (geçmiş YOK) | 🟩 |
| 3d | ↳ Kişiler (Contacts) | `raw/iphone/extract-2026-11-01/HomeDomain/Library/AddressBook/AddressBook.sqlitedb` | SQLite | 826 kişi → `person_alias` eşleşmesine yardım | 🟩 |
| 3f | ↳ SMS/iMessage | `raw/iphone/extract-2026-11-01/HomeDomain/Library/SMS/sms.db` | SQLite | 8.284 mesaj (2021-02'den), karşı taraf | 🟩 |
| 3g | ↳ WhatsApp (Business) | `raw/iphone/extract-2026-11-01/AppDomainGroup-group.net.whatsapp.WhatsAppSMB.shared/ChatStorage.sqlite` | SQLite + `Message/Media/` | 317.692 mesaj (2018-06'dan) | ✅ 313.172 mesaj (sistem mesajları + durumlar hariç), 9.456 ek dosya bağlı |
| 3h | ↳ Zepp/Amazfit saat | `raw/iphone/extract-2026-11-01/AppDomain-com.huami.watch/Documents/*/` | SQLite (`HMCorePersistanceDatabaseV1`, `healthMatrix`, `HMDBFirstBeatDataBaseV2`) | günlük adım/PAI/nabız, uyku, egzersiz | ✅ 520 gün, 359 gece, 42 egzersiz |
| 3j | ↳ Instagram ekran süresi | `raw/iphone/extract-2026-11-01/AppDomain-com.burbn.instagram/Documents/time_in_app_*.db` | SQLite | uygulamada geçen süre | 🟩 |
| 3e | Cihaz içi foto indeksleri (AFC dökümü) | `raw/iphone/afc-2026-11-01/PhotoData/Caches/…`, `MediaAnalysis/mediaanalysis.db` | SQLite | `psi.sqlite`: sahne etiketi, OCR, yer adı; `CLSLocationCache`: konum → yer adı; estetik skorları | 🟩 |
| 4 | Apple Music Play Activity | privacy.apple.com export → `raw/apple-privacy/` | CSV (`Apple Music - Play History Daily Tracks` / `Play Activity`) | her dinleme: zaman, parça, süre | ⬜ Tuna talep edecek |
| 5 | Snapchat My Data | `raw/snapchat/` | ZIP → JSON/HTML (memories_history, chat_history, ...) | memory'ler, snap/sohbet zamanları, karşı taraf | ⬜ Tuna talep edecek |
| 6 | BeReal GDPR | `raw/bereal/` | ZIP → JSON + görseller | her BeReal: zaman, ön/arka foto, konum | ⬜ Tuna talep edecek |
| 8 | **Google Calendar** (ana takvim) | `raw/google-calendar/events.json` (+ `changes.jsonl`, `state.json`) — **cfv yazar** (photos değil) | Google Calendar API JSON (gog, salt-okur) | tüm takvimler, tekrarlar açılmış, 2010 → bugün+400 gün; silinen olaylar `changes.jsonl`'da arşivlenir; tatil takvimi import'ta atlanır | ✅ sürekli senkron: `cfv-gcal-sync.timer` (30 dk), ~4.100 event |
| 7 | Google Takeout — YouTube | `raw/google-takeout/` | `watch-history.json` (JSON seç, HTML değil) | izleme zamanı, video başlığı, kanal | ⬜ Tuna talep edecek |

### Google Takvim senkronu
- `systemd --user` timer `cfv-gcal-sync.timer` her 30 dakikada `cfv sync gcal` çalıştırır
  (birimler: `src/systemd/`, kurulum `src/systemd/install.sh`).
- Tur: takvim başına `gog calendar changed --since <son kontrol>`; değişiklik varsa ya da son tam
  çekimden 24 sa geçtiyse tam çekim → fark `changes.jsonl`'a, `events.json` atomik yenilenir →
  `calendar-google` import + `timeline/` yeniden üretilir.
- gog yalnızca `--readonly --enable-commands=calendar` ile çağrılır; hesap `tunagul54@gmail.com`
  (gog: `~/.local/share/cfv-gog/gog`, sunucu girişinin kopyası). tunagul29 yalnız Gmail yetkili.
- Ağ yok / token düştü: log'a yazar, dosyalara dokunmaz, 0 ile çıkar. Olayların %20'sinden
  fazlası birden silinmiş görünürse yazmaz (yarım çekime karşı koruma).
- Log: `data/logs/gcal-sync.log` · durum: `uv run cfv sync gcal --status`.

### Gmail (`data/raw/gmail/<hesap>/`)

- `uv run cfv sync gmail` (elle; timer yok). Hesaplar `data/config/gmail-accounts` (git dışı).
- gog `--readonly --enable-commands=gmail`; yalnız başlıklar + snippet (`--format metadata`, gövde yok).
- `messages.jsonl` (mesaj başına bir satır) + `state.json`. İlk çekim ay ay, sonra son çekimin
  2 gün öncesinden `after:` ile yalnız yeniler. Kota dolunca 65 sn bekler.
- Import: `email` event'leri; SPAM/TRASH/DRAFT atlanır; kişiler gönderen/alıcı adresinden.
- Sınır: eski mesajlardaki etiket değişiklikleri (çöpe atma vb.) geri yansımaz.
- Log: `data/logs/gmail-sync.log`.

## Kapsam dışı

- Sesli notlar (Tuna, 2026-11-01: "kaldır") — ham veride durur, import edilmez.

## Notlar / bilinen kısıtlar

- **Şifresiz yedek** Sağlık, Anahtar Zinciri, Safari geçmişi ve bazı konum verilerini
  içermez. Bunlar istenirse şifreli yedek gerekir (şimdilik kapsam dışı).
- **Müzik geçmişi** yalnızca Apple'ın privacy exportunda var; telefon DB'si sadece toplam
  çalma sayısı ve son çalma zamanı verir. Dönem bazlı "o yaz ne dinledim" için #4 şart.
- **Google Takeout**'ta YouTube geçmişi için format olarak **JSON** seçilmeli
  (varsayılan HTML).
- **Foto eşleme:** Photos.sqlite asset'i ↔ diskteki dosya: `ZADDITIONALASSETATTRIBUTES.ZORIGINALFILENAME`
  + çekim zamanı (aynı dosya adı birden çok kez geçebilir: IMG_0001 tekrarları).
  icloudpd XMP sidecar'ı ek ipucu.
- Ham dosyaların sahibi photos ajanıdır; bu repo yalnızca okur.
- **`extract-*/` yedekteki dosyalara HARDLINK'tir.** Bir dosyaya yazmak yedeği de bozar →
  SQLite'lar yalnızca `file:...?mode=ro&immutable=1` URI'si ile açılır; WAL/journal oluşturulmaz.
- YouTube ve BeReal uygulama önbellekleri yedekte var ama tam geçmiş değil → Takeout / GDPR şart.

## Güncelleme kaydı

- 2026-11-01 — manifest oluşturuldu (Photos.sqlite hazır, yedek sürüyor).
- 2026-11-01 — yedek tamamlandı (22 GB), AFC dökümleri eklendi. Ayrıntılı ham envanter: `data/raw/README.md` (photos ajanı). Takvim kaynağı (yedek mi, iCloud/Google + vdirsyncer mi) henüz kararlaştırılmadı.
- 2026-11-01 — yedekten çıkarılan ağaç (`extract-2026-11-01/`) eklendi: notlar, takvim, kişiler, SMS, WhatsApp, Zepp, sesli notlar.
- 2026-11-01 — ilk importlar: fotolar, notlar+ödevler, iOS takvim, WhatsApp, Zepp. Takvimin ana kaynağı Google (Takeout/vdirsyncer bekleniyor). Sesli notlar kapsam dışı.
- 2026-11-01 — Google Calendar: gog ile salt-okur sürekli senkron (`data/raw/google-calendar/`, timer 30 dk). İlk çekim 4.257 olay (5 takvim).
