"""uv run python -m markov [--sims N]

Tüm günler üzerinde tek walk-forward geçiş: her t için önce tahmin, sonra t öğrenilir.
Yazar (data/processed/predict/):
  markov/pred_<split>.jsonl        nihai (hedef başına SOURCE'taki model)
  markov/{hour,day,mix}/           varyantlar (karşılaştırma için)
  markov/ceiling.json              Π_max ve gerçek sonraki-dilim doğruluğu
"""

from __future__ import annotations

import argparse
import json
import random

from common import SPLITS, cfv_db, common_db, out_dir, ro
from common import data
from common.evaluate import split_days, write_preds
from common.targets import COUNTS, known

from . import ceiling, slots
from .daylevel import DayLevel
from .hourly import Hourly
from .sets import Sets

def known_calendar(cfv, days: list[str]) -> dict[str, dict[str, float]]:
    """t başında takvimde zaten olan (t'den önce girilmiş) olayların sayısı ve saati."""
    out = {}
    for d in days:
        ev = [e for e in known(cfv, d)["calendar"] if not e["all_day"] and e["end_utc"]]
        h = sum(min(24.0, max(0.0, (slots.utc(e["end_utc"]) - slots.utc(e["start_utc"])).total_seconds() / 3600))
                for e in ev)
        out[d] = {"cal_events": len(ev), "cal_hours": min(24.0, h)}
    return out


# hedef → model ("hour" saatlik zincir, "day" gün düzeyi zincir, "mix" ikisinin ortalaması);
# dev sonuçlarına göre seçildi
SOURCE = {k: "day" for k in COUNTS} | {
    "msgs_sent": "hour", "msgs_recv": "hour", "screenshots": "hour",
    "sessions": "mix", "sessions_me": "mix", "photos": "mix"}
PLANNED = ("cal_events", "cal_hours")
# örnek kalibrasyonu: saatlik simülasyonun mesaj/oturum toplamları fazla dar (saatler arası bağımlılık
# eksik) → medyan etrafında SPREAD kat açılır. Sıra korunur (hours ile tutarlı). 2024-11..2025-04 ve
# dev'de seçildi: %10-90 kapsaması 0.61-0.71 → 0.77-0.85, CRPS bozulmadan.
SPREAD = {"msgs_sent": 1.4, "msgs_recv": 1.4, "sessions": 1.4, "sessions_me": 1.4}


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m markov")
    ap.add_argument("--sims", type=int, default=300)
    ap.add_argument("--samples", type=int, default=50, help="gün başına yazılan örnek gün sayısı")
    a = ap.parse_args()

    by_day = data.load_days()
    with ro(cfv_db()) as cfv, ro(common_db()) as com:
        sl = slots.load(cfv, com, min(d for d, r in by_day.items() if "msgs_sent" in r), max(by_day))
        # takvim: planlı kısım bilinir, zincir yalnız artığı (gerçek - planlı) modeller
        plan = known_calendar(cfv, [d for d, r in by_day.items() if "cal_events" in r])
    eval_days = {d: s for s in SPLITS for d in split_days(s, by_day)}
    first_pred = min(eval_days)
    hour_idx = {d: i for i, d in enumerate(sl["days"])}

    hourly = Hourly(sl, sims=a.sims, regimes=3, edge_end=hour_idx.get(first_pred))
    nxt = Hourly(sl, beta=16, half_life=365)  # yalnız sonraki-saat doğruluğu (Π_max karşılaştırması)
    resid = {d: {k: r[k] - plan[d][k] for k in plan[d]} for d, r in by_day.items() if d in plan}
    day_models = {k: DayLevel(resid if k in PLANNED else by_day, k, first_pred) for k in COUNTS}
    set_models = {k: Sets(by_day, k) for k in ("people", "chats")}
    acc = {s: {"hits": 0, "day_hits": 0, "total": 0} for s in SPLITS}
    preds = {s: {"markov": [], "markov/hour": [], "markov/day": [], "markov/mix": []} for s in SPLITS}
    samples = {s: [] for s in SPLITS}

    def draw(d: str, i: int, n: int, sims: list[dict] | None) -> list[dict]:
        """n örnek gün. Saatlik zincirin hedefleri (takvim hariç) aynı simülasyondan gelir (ortak);
        geri kalanlar gün düzeyi zincirden, kümeler öğe olasılıklarından bağımsız çekilir."""
        rng = random.Random(i)
        out = [dict(sims[k]) for k in range(n)] if sims else [{} for _ in range(n)]
        for o in out:
            for k in PLANNED:
                o.pop(k, None)
        if sims:
            for k, c in SPREAD.items():
                med = sorted(o[k] for o in out)[n // 2]
                for o in out:
                    o[k] = round(max(0.0, med + c * (o[k] - med)), 2)
        for k, m in day_models.items():
            if (sims and k in sims[0] and k not in PLANNED) or (k in PLANNED and d not in plan):
                continue
            for o, v in zip(out, m.sample(d, i, n, rng)):
                o[k] = round(v + plan[d][k], 2) if k in PLANNED else v
        for o in out:  # takvim sayısı ile saati ayrı çekilir; olay yoksa saat de yok
            if o.get("cal_events") == 0:
                o["cal_hours"] = 0.0
        for k, m in set_models.items():
            for o, v in zip(out, m.sample(d, i, n, rng)):
                o[k] = v
        return out

    for i, d in enumerate(sorted(by_day)):
        # saatlik zincir yalnız WhatsApp kapsamındaki günleri görür (boşluk ayları NULL, 0 değil)
        hi = hour_idx.get(d) if "msgs_sent" in by_day[d] else None
        if (split := eval_days.get(d)):
            h = {}
            if hi is not None:
                h0, d0 = nxt.hits, hourly.day_hits
                nxt.score_next(hi)
                h = hourly.predict(hi)
                acc[split]["hits"] += nxt.hits - h0
                acc[split]["day_hits"] += hourly.day_hits - d0
                acc[split]["total"] += 24
            dl = {k: v for k, m in day_models.items() if (v := m.predict(d, i)) is not None}
            for k in PLANNED:
                if k in dl:
                    dl[k] = round(dl[k] + plan[d][k], 2)
            st = {k: m.predict(d, i) for k, m in set_models.items()}
            mix = {k: round((h[k] + dl[k]) / 2, 2) for k in h if k in dl}
            final = {k: {"hour": h, "day": dl, "mix": mix}[SOURCE[k]].get(k, dl.get(k)) for k in COUNTS}
            preds[split]["markov"].append({"day": d, **{k: v for k, v in final.items() if v is not None}, **st})
            preds[split]["markov/hour"].append({"day": d, **h, **st})
            preds[split]["markov/day"].append({"day": d, **dl, **st})
            preds[split]["markov/mix"].append({"day": d, **mix, **st})
            samples[split].append({"day": d, "samples": draw(d, i, a.samples, h and hourly.samples)})
        if hi is not None:
            hourly.feed(hi)
            nxt.feed(hi)
        for m in day_models.values():
            m.feed(d, i)
        for m in set_models.values():
            m.feed(d, i)

    for split, by_name in preds.items():
        for name, rows in by_name.items():
            write_preds(out_dir(name) / f"pred_{split}.jsonl", rows)
        write_preds(out_dir("markov") / f"samples_{split}.jsonl", samples[split])

    # tavan dizisi: kapsanan günler uç uca (boşluk günleri atlanır)
    cov = [d for d in sl["days"] if "msgs_sent" in by_day.get(d, {})]
    pos = {d: j for j, d in enumerate(cov)}
    seq = "".join(chr(65 + s) for d in cov for s in sl["states"][24 * hour_idx[d]: 24 * hour_idx[d] + 24])
    lams = ceiling.lambdas(seq)
    ceil = {"all": ceiling.report(seq, lams)}
    for split in SPLITS:
        ds = [pos[d] for d in split_days(split, by_day) if d in pos]
        t = acc[split]["total"] or 1
        ceil[split] = ceiling.report(seq, lams, 24 * ds[0], 24 * (ds[-1] + 1)) | {
            "next_slot_acc": acc[split]["hits"] / t,   # gerçek geçmişle bir sonraki saat
            "day_ahead_acc": acc[split]["day_hits"] / t}  # 00:00'dan tüm günün saatleri
        c = ceil[split]
        c["next_over_pimax"] = c["next_slot_acc"] / c["Pi_max"]  # yaklaşımlar arası ortak ölçüt
        c["day_over_pimax"] = c["day_ahead_acc"] / c["Pi_max"]
    (out_dir("markov") / "ceiling.json").write_text(json.dumps(ceil, indent=1))
    print(json.dumps(ceil, indent=1))


if __name__ == "__main__":
    main()
