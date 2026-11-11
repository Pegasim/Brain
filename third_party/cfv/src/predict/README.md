# cfv-predict

Tuna'nın sonraki gününü tahmin etmek. İş tanımı ve kurallar: `docs/predict.md`.

    cd src/predict
    uv run python -m common all            # build + baselines + eval (dev)
    uv run python -m common build          # common.sqlite'ı yeniden üret (veri değişince)
    uv run python -m common eval --split test [yaklaşım...]
    uv run pytest

## Ortak tablo (`data/processed/predict/common.sqlite`)

- `session`: WhatsApp oturumları (aynı sohbette 30 dk sessizlik → yeni oturum); `n_msgs`,
  `n_me`, `people` (person id JSON), `day` = ilk mesajın yerel günü.
- `day`: gün başına hedefler. Kaynak o günü kapsamıyorsa NULL (0 değil).
  - sayılar: `msgs_sent msgs_recv sessions sessions_me photos screenshots todo_done todo_open
    cal_events cal_hours steps sleep_min`
  - kümeler (JSON): `people` (benim yazdığım DM'lerin karşı tarafı), `chats` (yazdığım
    sohbetler), `projects` (gün raporundan)
  - metin: `summary` (gün raporu başlığı)
- `common.targets.known(conn, gün)`: t başında bilinenler (haftanın günü, t'den önce girilmiş
  takvim). Ödevlerin `done` bayrağı notun son hâlidir: t için girdi olamaz, geçmiş günlerin
  `todo_done/todo_open` değerleri de sonradan işaretlenmiş olabilir (bilinen sızıntı, rakipleri de
  aynı ölçüde etkiler; `compact day` dökümündeki [x]/[ ] de öyle).

## Walk-forward değerlendirme

Dönemler `common.SPLITS`: **dev** 2025-05-01..2025-10-31 (ayar burada), **test**
2025-11-01..son gün (yalnız rapor). t satırını üreten şey t'yi ve sonrasını görmez
(`common.data.history(by_day, t)`); periyodik yeniden eğitim serbest, yeter ki eğitim < t.

Yaklaşım `data/processed/predict/<ad>/pred_<split>.jsonl` yazar, gün başına bir satır:

    {"day": "2025-11-03", "msgs_sent": 41, "people": ["12", "88"]}

`eval` her yaklaşımı rakiplerle (`baseline_yesterday`, `baseline_dow4`) aynı günlerde ölçer:
sayılarda MAE ve en iyi rakibe oran (<1 = geçti), kümelerde Jaccard. Tablo:
`data/processed/predict/leaderboard_<split>.md`.

## Yaklaşımlar

Her yaklaşım `src/predict/<ad>/` altında (ör. `markov/`), çıktısı `data/processed/predict/<ad>/`.
Ağır bağımlılıklar (torch vb.) `pyproject.toml`'da yaklaşıma ait dependency group'a.
GPU tek: eğitimi `flock ../../data/processed/predict/gpu.lock <komut>` ile koş (sıra kendiliğinden).
