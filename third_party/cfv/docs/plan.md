# Yol haritası

## Faz 0 — Temel ✅ (2026-11-01)
Repo iskeleti, `.gitignore`, uv/pyproject, README, veri manifestosu, şema, sentetik testler.

## Faz 1 — İlk kaynaklar + temel arama ✅ (2026-11-01)
- Okuyucular (`src/cfv/sources/`): `photos` (Photos.sqlite + icloud-sync eşleme),
  `notes` (protobuf çözücü + **ödev ayrıştırıcı**: hafta/gün/bölüm/tik), `calendar_ios`,
  `calendar_ics` (Google için), `whatsapp`, `zepp` (adım/PAI, uyku, egzersiz).
- `cfv tree` → `data/processed/timeline/YYYY/AA-Ay/Wnn (gg-gg)/` (README + whatsapp.md +
  notlar/ + fotolar/ sembolik linkleri).
- Temel arama: FTS5 (Türkçe katlama, önek), `when` = eşleşmeleri haftalara toplama.
- MCP (`cfv-mcp`, salt-okur): `when`, `search`, `timeline`, `digest`, `week_page`, `people`, `photos`.

## Faz 1.1 — Google Takvim ✅ (2026-11-01)
- gog ile salt-okur, artımlı sürekli senkron (`cfv-gcal-sync.timer`, 30 dk) + `calendar-google` okuyucu.

## Faz 2 — Anlamsal (vektör) arama ← SIRADAKİ
Hedef: ajanlar "ne zaman şu işle uğraştım?" sorusunu kelime tutmasa da anlamla bulsun.
- **Yerel** embedding modeli (çok dilli, Türkçe iyi: bge-m3 / multilingual-e5 sınıfı), GPU'da.
  Hiçbir metin dışarı gitmez.
- **Parçalama (chunk)**: tek tek WhatsApp mesajı anlamsız → sohbet başına zaman pencereleri
  (ör. 30 dk sessizlikle bölünen konuşma dilimleri); ödevler gün başına; not paragrafları;
  takvim olayı tek parça; fotolar için kişi+yer+albüm (+ ileride Apple sahne etiketleri/OCR).
- Şema: `chunk(id, kind, start_utc, end_utc, local_date, text, event_ids)` +
  `chunk_vec` (sqlite-vec). İlişkisel kalır; vektör sadece ek bir indeks.
- Arama: hibrit = FTS (bm25) + vektör (kosinüs) → RRF birleştirme → haftalara toplama.
- MCP: `semantic_search(query, start?, end?, kinds?)` + `when` hibrit moda geçer.
- Oynama alanı: model/parça boyu karşılaştırma için küçük bir değerlendirme seti
  (Tuna'nın gerçek soruları + beklenen haftalar).

## Faz 3 — Daha fazla kaynak
- Apple Music Play Activity (privacy.apple.com), Snapchat, BeReal, YouTube (Takeout JSON).
- Yedekte duran ama henüz alınmayanlar: SMS/iMessage, Kişiler (person birleştirme için),
  Anımsatıcılar, Apple foto arama indeksi `psi.sqlite` (sahne etiketi, OCR, yer adları).

## Faz 4 — Zenginleştirme
- Kişi birleştirme: Photos adı ↔ WhatsApp ↔ Kişiler (öneri üret, onayı Tuna verir).
- Yer kümeleme / yerel ters geocoding; Immich (Tuna kararına bağlı; CLIP ile görsel arama).
- Instagram postu akışı: dönem → aday fotolar + o dönemin müziği + takvim + notlar.
