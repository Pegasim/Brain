# life2vec

Olay embedding'leri + gün düzeyi GRU ("şimdiki hâl" vektörü) → sonraki gün. Referans: life2vec,
RMTPP, Neural Hawkes. Kurallar: `docs/predict.md`; ortak altyapı: `src/predict/README.md`.

    cd src/predict
    nice -n 19 env OMP_NUM_THREADS=6 flock ../../data/processed/predict/gpu.lock \
      uv run --group life2vec python -m life2vec --split dev test   # 3 seed, haftalık yeniden eğitim
    uv run python -m common eval --split dev
    uv run --group life2vec pytest

Çıktı: `data/processed/predict/life2vec/pred_<split>.jsonl` (nokta) ve `samples_<split>.jsonl`
(gün başına 100 olası gün; sayılar). Bir model GPU'da ~2 sn; dev+test, 3 seed ~5 dk.

## Mimari

- **Olay** = WhatsApp oturumu: sohbet embedding'i + kişi embedding'lerinin ortalaması + saat
  (döngüsel), log mesaj / benden / süre, DM bayrağı. Günün olayları ortalanır.
- **Gün girdisi** (gün bitince bilinen): log1p sayılar + eksik bayrağı (NULL ≠ 0), oturum havuzu,
  bağlam. Ödevlerde yalnız toplam: `done` bayrağı notun son hâli, geçmişte de sızar.
- **Bağlam** (t başında bilinen): haftanın günü, t'ye önceden girilmiş takvim (sayı, saat, ilk
  başlangıç, son bitiş, sabah/öğle/akşam saatleri, tüm gün olayları).
- **GRU** tüm geçmiş üzerinde; t'yi `h[t-1]` + bağlam[t] tahmin eder.
- **Sayılar**: negatif binom (μ ve dispersiyon güne bağlı), adım/uyku log1p ölçeğinde Laplace
  (uykuda 16-25 saatlik aykırı kayıtlar var). log μ'ye hedef başına tazelik terimi eklenir: son
  bilinen değer, 7/28 gün ve son 4 aynı gün ortalaması, 28 günde dolu oranı, son bilinenin yaşı.
  Nokta = karışımın medyanı (MAE için en iyisi), örnekler aynı dağılımdan.
- **Kümeler** (people, chats): öğe başına logit = `q(z)·E[öğe]` + öğe biası + paylaşılan
  tazelik MLP'si + öğeye özgü tazelik ağırlığı (`E[öğe]`'den). Tazelik: dün, evvelki gün, geçen
  hafta aynı gün, 7/28/180 gün oranı, son 4 aynı gün, son görülmeden beri, dün gelen / cevapsız
  oturum, 7 günde gelen. Seçim: beklenen Jaccard'ı en büyükleyen top-k.
- **Walk-forward**: her hafta başında (cut) sözlük ve ağırlıklar yalnız < cut ile; son 42 gün
  erken durma için. Tazelik özellikleri t için yalnız < t'yi görür (`tests/test_life2vec.py`).

## Sonuçlar (MAE / en iyi rakip; kümelerde Jaccard)

| hedef | dev | test |
|---|---|---|
| msgs_sent / msgs_recv | 0.90 / 0.92 | 1.00 / 0.97 |
| sessions / sessions_me | 0.97 / 0.91 | 0.98 / 0.95 |
| photos / screenshots | 0.81 / 0.75 | 0.86 / 0.87 |
| todo_done / todo_open | 0.86 / 1.20 | 0.85 / 0.81 |
| cal_events / cal_hours | 0.72 / 0.70 | 0.75 / 0.72 |
| steps / sleep_min | 0.94 / 0.79 | 0.89 / 0.78 |
| people / chats | J 0.342 / 0.302 | J 0.344 / 0.335 |

Test CRPS: 12 sayının 11'inde rakiplerden iyi; adımda 1.09.

## Bilinen sınırlar

- Sözlük dışı (cut'tan sonra ilk kez görülen) kişi/sohbet tahmin edilemez; haftalık yeniden
  eğitim bunu küçültür.
- Adım örnekleri geniş (log-Laplace kuyruğu; geçmiş en büyüğün 2 katında kırpılır) → adım CRPS'i zayıf.
- Takvim ve mesaj sayılarında markov önde; `ensemble` ikisini hedef başına birleştirir.
- GPU sonuçları seed sabitken de koşudan koşuya ±0.01 oynar (cuDNN).
- Metin/foto embedding'leri ve `projects`/`summary` henüz yok.
