"""Yaklaşımların birleşimi: hedef başına ağırlık dev'de seçilir, test'e aynen uygulanır.

Sayılar: Σ w_i·tahmin_i, w 0.25 adımlı simplex üzerinde (dev MAE'si en düşük; o gün tahmini olmayan
yaklaşımın payı ötekilere dağılır). Örnekler: aynı oranlarda karışım (hedef başına ayrı, yani örnekler
hedefler arası tutarlı değil). Kümeler: dev'de Jaccard'ı en yüksek yaklaşım.
    uv run python -m ensemble [yaklaşım...]   (varsayılan: markov life2vec persona)
"""

from __future__ import annotations

import itertools
import random
import sys

from common import data, out_dir
from common.evaluate import read_preds, read_samples, score, split_days, write_preds
from common.targets import COUNTS, SETS

STEP = 4  # ağırlık adımı 1/STEP


def simplex(n: int) -> list[tuple[float, ...]]:
    return [tuple(c / STEP for c in cs) for cs in itertools.product(range(STEP + 1), repeat=n)
            if sum(cs) == STEP]


def blend(ps: list[dict], w: tuple[float, ...], k: str) -> float | None:
    vw = [(p[k], wi) for p, wi in zip(ps, w) if p.get(k) is not None]
    tot = sum(wi for _, wi in vw)
    if not vw:
        return None
    if tot == 0:  # payı olan yaklaşımların hiçbiri o gün tahmin etmemiş → eşit ortalama
        return sum(v for v, _ in vw) / len(vw)
    return sum(v * wi for v, wi in vw) / tot


def mix(ss: list[list[dict]], w: tuple[float, ...], n: int, rng: random.Random) -> list[dict]:
    pairs = [(s, wi) for s, wi in zip(ss, w) if s]
    tot = sum(wi for _, wi in pairs)
    if not pairs:
        return []
    if tot == 0:
        pairs, tot = [(s, 1.0) for s, _ in pairs], len(pairs)
    return [rng.choice(s) for s, wi in pairs for _ in range(round(n * wi / tot))]


def main() -> None:
    names = sys.argv[1:] or ["markov", "life2vec", "persona"]
    by_day = data.load_days()
    path = lambda x, s: out_dir(x) / f"pred_{s}.jsonl"
    splits = [s for s in ("dev", "test") if all(path(x, s).exists() for x in names)]
    assert "dev" in splits, "her yaklaşımın pred_dev.jsonl'i olmalı"
    P = {(x, s): read_preds(path(x, s)) for x in names for s in splits}
    S = {(x, s): read_samples(out_dir(x) / f"samples_{s}.jsonl") for x in names for s in splits}
    dev = split_days("dev", by_day)

    w = {}
    for k in COUNTS:
        def mae(wk):
            p = {d: {k: blend([P[x, "dev"].get(d, {}) for x in names], wk, k)} for d in dev}
            m = score(p, by_day, dev, k)
            return m["mae"] if m else float("inf")
        w[k] = min(simplex(len(names)), key=mae)
    for k in SETS:
        j = [(score(P[x, "dev"], by_day, dev, k) or {}).get("jaccard", -1) for x in names]
        w[k] = tuple(float(i == j.index(max(j))) for i in range(len(names)))
    print("ağırlıklar", names)
    for k, v in w.items():
        print(f"  {k}: {v}")

    for s in {"dev", "test"} - set(splits):
        for f in ("pred", "samples"):
            (out_dir("ensemble") / f"{f}_{s}.jsonl").unlink(missing_ok=True)
        print(f"{s}: bazı yaklaşımların tahmini yok, atlandı")
    rng = random.Random(0)
    for s in splits:  # tahmini eksik split yazılmaz (eski dosya kalmasın diye silinir)
        preds, samples = [], []
        for d in split_days(s, by_day):
            ps = [P[x, s].get(d, {}) for x in names]
            r = {"day": d}
            for k in COUNTS:
                if (v := blend(ps, w[k], k)) is not None:
                    r[k] = round(v, 2)
            for k in SETS:
                if (v := ps[w[k].index(1.0)].get(k)) is not None:
                    r[k] = v
            preds.append(r)
            ss = [S[x, s].get(d, []) for x in names]
            if any(ss):
                n = max(len(x) for x in ss)
                cols = {k: mix([[x for x in sx if k in x] for sx in ss], w[k], n, rng) for k in COUNTS}
                m = min((len(c) for c in cols.values() if c), default=0)
                samples.append({"day": d, "samples": [{k: c[i][k] for k, c in cols.items() if c}
                                                      for i in range(m)]})
        write_preds(out_dir("ensemble") / f"pred_{s}.jsonl", preds)
        write_preds(out_dir("ensemble") / f"samples_{s}.jsonl", samples)


if __name__ == "__main__":
    main()
