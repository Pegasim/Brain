# CFV tahmin (prediction) — iş tanımı

Amaç: Tuna'nın **sonraki gününü** tahmin etmek; LLM'in "sonraki kelime"si yerine "sonraki gün".
Uzun vadeli hedef: Tuna'yı simüle eden bir **beyin modeli** ve onu çevresiyle (insanlar, okul,
projeler, takvim) birlikte simüle eden bir **world model**: ben + env birlikte ilerler.

## Ajanlar (bp)
- `cfv-predict` (koordinatör): ortak altyapıyı (veri seti, değerlendirme) kurar, yaklaşımları
  karşılaştırır, sonuçları `bp msg server-cfv` ile bildirir. Alt ajanları o açar ve yönetir.
- `cfv-predict-markov`: Markov zincirleri (1-2. derece, saat/gün bağlamlı, backoff) + tahmin
  tavanı (Song ve ark. 2010: Lempel-Ziv entropisi + Fano). En güçlü basit rakip.
- `cfv-predict-life2vec`: olay embedding'leri (tür/kişi/sohbet tabloları, döngüsel zaman + log Δt,
  metin/foto için dondurulmuş yerel embedding) + sıralı model. RNN/GRU önce (düzensiz zaman,
  "şimdiki hâl" vektörü); gerekirse Mamba/transformer. Referans: life2vec, RMTPP, Neural Hawkes.
- `cfv-predict-persona`: Tuna'nın yerine geçen LLM. Girdi: t'den önceki hiyerarşik hafıza +
  t'nin takvimi → t'nin raporu. LLM işi bp ile açılan **Haiku** ajanlarına yaptırılır
  (`bp open <ad> <klasör> --claude --parent cfv-predict-persona -- --model haiku`).

## Ortak kurallar
- **Walk-forward:** t gününü tahmin eden hiçbir şey t'yi ya da sonrasını görmez. t'yi kapsayan
  hafta/ay/yıl raporları YASAK; yalnız t'den önce biten düğümlerin raporları kullanılır.
- **Ortak hedefler** (karşılaştırılabilsin): günlük sayılar (gönderilen/gelen mesaj, sohbet
  oturumu, foto, yapılan/açık ödev, takvim saati, adım, uyku), kümeler (konuşulan kişiler,
  aktif sohbetler, projeler), metin (gün özeti). Her yaklaşım aynı tabloyu doldurur.
- **Rakipler her zaman yanında:** "yarın = dün", "son 4 aynı günün ortalaması". Bunları geçmeyen
  sonuç anlamlı değildir.
- **Sohbet oturumları:** 313k WhatsApp mesajı olay olarak verilmez; aynı sohbette 30 dk'dan uzun
  sessizlik → yeni oturum (sohbet, kişiler, başlangıç/bitiş, mesaj sayısı, benden kaç).

## Veri ve kod
- Okuma: `data/processed/cfv.sqlite` (salt-okur aç: `file:...?mode=ro`), raporlar
  `data/processed/reports/`, `./bin/compact input|day`. `data/raw/` DOKUNMA/YAZMA.
- Yazma: kod `src/predict/<yaklaşım>/`, çıktı `data/processed/predict/<yaklaşım>/`.
- Repo PUBLIC: kodda/testte kişisel veri yok (fixture'lar sentetik). git commit/push yapma;
  `cfv`'ye bildir, o inceleyip commit eder.
- Kod küçük ve sade (Tuna: "bloat olmasın"). ML için Python/PyTorch serbest; bağımlılıklar
  `src/predict/` altında ayrı uv projesinde (ana projeyi şişirme).
- Donanım: RTX 3070 Laptop 8 GB, 38 GB RAM, 16 çekirdek. GPU tek: aynı anda tek eğitim
  (koordinatör sıraya koyar). **GPU'da LLM çalıştırma** (ollama/llama yok; Tuna, 2 Kas 2026).
  LLM gereken her iş Claude'a (bp Haiku ajanları); Tuna veri göndermeyi onayladı.
- Yeni veri hâlâ geliyor (fotolar inmekte); deneyler veri değişince yeniden koşabilmeli.
