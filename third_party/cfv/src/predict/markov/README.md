# markov

Sonraki günü Markov zincirleriyle tahmin eder. Rakip olarak en güçlü basit yöntem ve olasılıksal
referans budur. Yalnız CPU kullanır, bağımlılığı yoktur.

    cd src/predict
    uv run python -m markov            # walk-forward (dev + test), ~15 sn
    uv run python -m common eval markov [--split test]
    uv run pytest tests/test_markov.py

Çıktı (`data/processed/predict/markov/`):
- `pred_<split>.jsonl`: nokta tahmin.
- `samples_<split>.jsonl`: gün başına 50 örnek gün. Her örnekte `hours` (24 harfli saatlik durum
  yolu), 12 sayı hedefi ve `people`/`chats` var.
- `ceiling.json`: tahmin tavanı ve gerçek doğruluk.
- `hour/`, `day/`, `mix/`: karşılaştırma varyantları.
- `report.md`: ayrıntılı sonuçlar (yerel, repoda değil).

## Yöntem

- `slots.py`: saatlik durum dizisi (yerel saat UTC+3). Mesaj (yok / yalnız gelen / benden) × foto ×
  takvim = 12 durum; her saat diliminin bir de emisyon vektörü var (o saatte başlayan sayılar).
- `chain.py`: bağlamlı zincir. Seyrek bağlamda Dirichlet backoff, unutma yarı-ömürle.
- `hourly.py`: 2. derece saatlik zincir; bağlam saat, hafta içi/sonu ve haftanın günü. Gizli bir gün
  rejimi var: günün mesaj hacminin tersili, kendisi de bir zincir. Rejim her örnek gün için ayrı
  çekilir ve gün boyu sabit kalır. t-1'in son iki saatinden başlayıp 24 saat simüle eder; her dilim
  için yakın geçmişten bir emisyon çeker. Nokta tahmin = örnek toplamların medyanı.
- `daylevel.py`: gün düzeyi zincir. Her sayı kantil kutularına bölünür; bağlam haftanın günü, dünün
  kutusu ve geçen haftanın aynı günü. Takvimde planlı kısım (`common.targets.known`, t'den önce
  girilmiş olaylar) bilinir, zincir yalnız planla gerçek arasındaki farkı modeller.
- `sets.py`: people/chats için öğe başına "bugün var mı" zinciri. Bağlam: dün, önceki gün, geçen
  hafta, son görülmeden beri geçen süre. Küme, beklenen Jaccard'ı en büyük yapan ilk k öğedir.
- `ceiling.py`: Song ve ark. (2010) tavanı. Saatlik dizinin LZ entropisi + Fano'dan Π_max.
- `__main__.py`: tek walk-forward geçiş (önce tahmin, sonra öğren). Hedef başına model seçimi
  (`SOURCE`): mesajlar saatlik zincirden, oturum ve foto iki zincirin ortalamasından, geri kalanlar
  gün düzeyinden. Örnek kalibrasyonu (`SPREAD`): mesaj ve oturum örnekleri medyan etrafında 1.4 kat
  açılır; sıra korunduğu için `hours` ile tutarlı kalır.

Parametreler dev'de (2025-05..10) seçildi, bir kısmı 2024-11..2025-04'te ayrıca kontrol edildi.
Test yalnız rapor içindir. Davranış hızlı değiştiği için kısa bellek açık ara iyi geldi: saatlik
zincirde yarı-ömür 7 gün ve son 10 emisyon, gün düzeyinde kutu başına son 10 değer.

## Sonuçlar (2026-11-02, en iyi rakibe oran; < 1 = geçti)

| | dev | test |
|---|---|---|
| MAE, sayılar | todo_open hariç hepsi 0.71–0.97 | hepsi 0.53–0.95 |
| CRPS, sayılar | 0.55–0.93 | 0.47–0.89 |
| Jaccard, people / chats | 0.320 / 0.300 (dün 0.306 / 0.282) | 0.325 / 0.322 (dün 0.315 / 0.316) |
| %10–90 aralığın kapsaması, mesaj/oturum | 0.77–0.85 | 0.76–0.86 |
| sonraki saat doğruluğu / Π_max | 0.48 / 0.68 = 0.71 | 0.44 / 0.65 = 0.68 |

Takvimde en büyük kazanç planlı olaylardan geliyor (test MAE oranı ~0.55).

## Bilinen sınırlar

- `todo_open` dev'de kaybediyor (1.28). 2025-05..07'de "dün" ile berabere; kaybın tamamı okul
  açılışındaki seviye sıçramasında (2025-09..10, önceki ay NULL). Ödevlerde `done` bayrağı notun son
  hâli, yani "açık" = hiç işaretlenmemiş (sızıntı ya da not tutma alışkanlığı).
- Örnekler arası tutarlılık kısmi. Saatlik hedefler ile `hours` ortak; takvim, ödev, adım, uyku ve
  kümeler ise bağımsız çekiliyor. Kişi ve sohbet örnekleri saatlik simülasyona bağlı değil; world
  model'e geçişte sıradaki adım bu.
- Kalibrasyon simülasyonun dışında yapılıyor (sonradan açma). Gün rejimi zaten her örnek için ayrı
  çekiliyor. Emisyonlar parametrik değil, yakın geçmişten seçiliyor; bu yüzden NB/gamma-Poisson
  eklemek ayrı bir model değişikliği olur, denenmedi.
- `sleep_min` örnekleri test'te dar (kapsama ~0.66).
- Yerel saat sabit UTC+3; yurtdışı günlerinde saatler kayar.
- LZ tahmincisi kısa dizide entropiyi düşük tahmin eder, bu yüzden Π_max iyimser bir üst sınırdır.
  Kümelerde "dün sohbeti kim başlattı" bilgisi denendi, etkisi olmadı (±0.001), çıkarıldı.
