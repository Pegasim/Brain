# cfv-site (`src/site/`)

Tuna'nın hayat verisi: soldan sağa zaman çizgisi, çevresinde Obsidian tarzı ağ (kişi, WhatsApp grubu,
takvim, foto kümesi, not). Kişi paneli, eşleşmeler (onayla / reddet / geri al). Plan: `/srv/cfv/site/PLAN.md`.

**Durum (2026-11-09): cfv.tunapro.xyz için YALNIZ SENTETİK veri** (`CFV_SITE_REQUIRE_SYNTHETIC=1`).
Gerçek veri bağlamak ayrı karar, Tuna onayı ister. Kod pyproject'e bağlı değil: `src/site/cfv-site` başlatıcısı
repo `.venv`'ini ve `PYTHONPATH=src/site` kullanır (`cfv` paketi oradan import edilir).

```sh
PYTHONPATH=src/site .venv/bin/python -m cfv_site.synth /yol/cfv.sqlite [--scale 1]   # SENTETİK örnek DB
src/site/cfv-site set-password      # terminalde, getpass; hash /var/lib/cfv-site/password (CFV_SITE_STATE)
src/site/cfv-site serve             # 127.0.0.1:3050; veri kökü CFV_DATA
uv run pytest tests/test_web.py
```
⛔ İPTAL (2026-11-09): SMS işi server-sms'e geçti; aşağıdaki uç cfv-site'a BAĞLI DEĞİL, `cfv_site/sms.py` yalnız duruyor.
SMS ucu (`POST /ingest/sms`, iPhone Kestirmeler, `Authorization: Bearer <token>`, gövde `{sender, text, received_at}`):
ham kayıt `CFV_SMS_DIR/YYYY-MM.jsonl` (yalnız ekleme, 600). `/var/lib/cfv-site/sms-token` yoksa uç KAPALI (503);
`src/site/cfv-site sms-token` token'ı Tuna'nın terminalinde üretir, yalnız ekrana basar, sunucuda sha256'sı kalır.
10 dk içinde aynı gönderen+metin yazılmaz (200 `tekrar`); tek kullanımlık kod (OTP) SMS'i hiç yazılmaz, yalnız
aylık sayaç. Metin loglanmaz. Ayrıntı: `cfv_site/sms.py`.

Önizlemeler: önce `data/processed/photos/small/<asset-uuid>.jpg` (224 px) ve `photos/thumbs/<uuid>.jpg`
(768 px); yoksa istek anında vips/ffmpeg ile `processed/site-thumbs/` altına üretilir.
Bakmak için: `ssh -L 3050:127.0.0.1:3050 sunucu` → http://localhost:3050

| Dosya | Ne |
|---|---|
| `server.py` | stdlib HTTP; giriş dışında her yol oturum ister; loglarda sorgu dizesi yok |
| `auth.py` | scrypt hash (`/var/lib/cfv-site/password`), sunucu tarafı oturum, IP kilidi |
| `data.py` | cfv.sqlite **salt-okur**; günlük sayımlar bellekte, pencere/kişi/eşleşme yanıtları |
| `match.py` | eşleşme önerileri (telefon, isim) + union-find; `person_match` tablosu varsa onu kullanır |
| `decisions.py` | `data/config/decisions.sqlite` — Tuna'nın kararları, alias'a bağlı, satır silinmez |
| `synth.py` | sentetik DB üreteci |
| `static/` | tek sayfa (canvas + d3-force, ISC, gömülü; dış istek yok) |
| `cfv-site.service` | sunucu birimi (kurulmadı) |

Kararlar `(source_kind, alias)` çiftine bağlıdır, `person.id`'ye değil: ingest DB'yi baştan üretse de korunur.
'different' kararı iki kaydı dolaylı yoldan da birleştirmez. Eşik (0.9) üstü öneriler (telefon) onaysız
birleşir, sitede "otomatik" görünür, "Ayır" ile reddedilir.
