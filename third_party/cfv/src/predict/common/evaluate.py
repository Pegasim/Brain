"""Walk-forward değerlendirme.

Tahmin dosyası: data/processed/predict/<yaklaşım>/pred_<split>.jsonl, gün başına bir satır:
    {"day": "2025-11-03", "msgs_sent": 41, "people": ["12", "88"], ...}
Yalnız tahmin edilen hedefler yazılır. t satırı yalnız t'den önceki veriyle üretilmiş olmalı.

Metrikler gerçek değeri olan ve tahmin edilmiş günlerde, rakipler AYNI günlerde ölçülür:
- sayılar: MAE; `ratio` = ortak günlerde MAE / en iyi rakibin MAE'si (<1 rakibi geçti)
- kümeler: ortalama Jaccard (ikisi de boşsa 1), precision, recall

Olasılıksal (opsiyonel): <yaklaşım>/samples_<split>.jsonl, gün başına olası günler:
    {"day": "2025-11-03", "samples": [{"msgs_sent": 38, ...}, {"msgs_sent": 52, ...}]}
Sayılarda örnek tabanlı CRPS (nokta tahminde = MAE) ve %10-90 aralığının kapsaması (ideal 0.80).
Rakiplerin örnekleri: dün → tek nokta, dow4 → son 4 aynı günün kendisi.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import SPLITS, out_dir
from .baselines import BASELINES
from .targets import COUNTS, SETS


def split_days(split: str, by_day: dict[str, dict]) -> list[str]:
    a, b = SPLITS[split]
    b = b or max(by_day)
    return sorted(d for d in by_day if a <= d <= b)


def read_preds(path: Path) -> dict[str, dict]:
    with path.open() as f:
        return {(r := json.loads(line))["day"]: r for line in f if line.strip()}


def write_preds(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_samples(path: Path) -> dict[str, list[dict]]:
    if not path.exists():
        return {}
    with path.open() as f:
        return {(r := json.loads(line))["day"]: r["samples"] for line in f if line.strip()}


def crps(xs: list[float], y: float) -> float:
    """Örnek tabanlı CRPS: E|X-y| - E|X-X'|/2."""
    xs = sorted(xs)
    n = len(xs)
    spread = sum((2 * i - n + 1) * x for i, x in enumerate(xs)) * 2 / (n * n)
    return sum(abs(x - y) for x in xs) / n - spread / 2


def _vals(samples: dict[str, list[dict]], d: str, k: str) -> list[float]:
    return [v for s in samples.get(d, []) if (v := s.get(k)) is not None]


def score_samples(samples, truth, days, k) -> dict | None:
    ds = [d for d in days if truth[d].get(k) is not None and _vals(samples, d, k)]
    if not ds:
        return None
    c, cov = 0.0, 0
    for d in ds:
        xs, y = sorted(_vals(samples, d, k)), truth[d][k]
        c += crps(xs, y)
        cov += xs[int(0.1 * (len(xs) - 1))] <= y <= xs[round(0.9 * (len(xs) - 1))]
    return {"n": len(ds), "crps": c / len(ds), "cov80": cov / len(ds), "days": ds}


def _jacc(p: set, t: set) -> tuple[float, float, float]:
    inter = len(p & t)
    j = 1.0 if not p and not t else inter / len(p | t)
    prec = inter / len(p) if p else float(not t)
    rec = inter / len(t) if t else 1.0
    return j, prec, rec


def score(preds: dict[str, dict], truth: dict[str, dict], days: list[str], k: str) -> dict | None:
    ds = [d for d in days if truth[d].get(k) is not None and preds.get(d, {}).get(k) is not None]
    if not ds:
        return None
    if k in SETS:
        s = [_jacc(set(preds[d][k]), set(truth[d][k])) for d in ds]
        return {"n": len(ds), "jaccard": sum(x[0] for x in s) / len(s),
                "precision": sum(x[1] for x in s) / len(s), "recall": sum(x[2] for x in s) / len(s)}
    return {"n": len(ds), "mae": sum(abs(preds[d][k] - truth[d][k]) for d in ds) / len(ds), "days": ds}


def evaluate(approach: str, split: str, by_day: dict[str, dict], days: list[str] | None = None) -> dict:
    """`days` verilirse (alt küme) yalnız o günler ölçülür ve eval_<split>.json yazılmaz."""
    subset = days is not None
    days = days if subset else split_days(split, by_day)
    preds = read_preds(out_dir(approach) / f"pred_{split}.jsonl")
    base = {b: read_preds(out_dir(b) / f"pred_{split}.jsonl") for b in BASELINES}
    res = {}
    for k in COUNTS + SETS:
        m = score(preds, by_day, days, k)
        if not m:
            continue
        ds = m.pop("days", None)
        if ds is not None:  # oran: yaklaşımın ve tüm rakiplerin tahmin ettiği ortak günlerde
            common = [d for d in ds if all(p.get(d, {}).get(k) is not None for p in base.values())]
            if common:
                own = score(preds, by_day, common, k)["mae"]
                best = min(score(p, by_day, common, k)["mae"] for p in base.values())
                m["ratio"] = own / best if best else None
        res[k] = m
    samples = read_samples(out_dir(approach) / f"samples_{split}.jsonl")
    if samples:
        bs = {b: read_samples(out_dir(b) / f"samples_{split}.jsonl") for b in BASELINES}
        for k in COUNTS:
            m = score_samples(samples, by_day, days, k)
            if not m or k not in res:
                continue
            ds = m.pop("days")
            res[k] |= {"crps": m["crps"], "cov80": m["cov80"], "crps_n": m["n"]}
            common = [d for d in ds if all(_vals(b, d, k) for b in bs.values())]
            if common:
                best = min(score_samples(b, by_day, common, k)["crps"] for b in bs.values())
                res[k]["crps_ratio"] = score_samples(samples, by_day, common, k)["crps"] / best if best else None
    out = {"approach": approach, "split": split, "days": [days[0], days[-1]] if days else [],
           "n_pred": len(preds), "targets": res}
    if not subset:
        (out_dir(approach) / f"eval_{split}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    return out


def table(results: list[dict], metric: str = "mae") -> str:
    """Yaklaşım × hedef tablosu (markdown). mae: sayılarda MAE (oran), kümelerde Jaccard; crps: CRPS (oran) [kapsama]."""
    keys = [k for k in (COUNTS + SETS if metric == "mae" else COUNTS)
            if any(metric in r["targets"].get(k, {}) or (k in SETS and k in r["targets"]) for r in results)]
    lines = ["| hedef | " + " | ".join(r["approach"] for r in results) + " |",
             "|---|" + "---|" * len(results)]
    rk = "ratio" if metric == "mae" else "crps_ratio"
    for k in keys:
        cells = []
        for r in results:
            m = r["targets"].get(k)
            if not m or (k not in SETS and metric not in m):
                cells.append("–")
            elif k in SETS:
                cells.append(f"J {m['jaccard']:.3f}")
            else:
                ratio = f" ({m[rk]:.2f})" if m.get(rk) else ""
                cov = f" [{m['cov80']:.2f}]" if metric == "crps" else ""
                cells.append(f"{m[metric]:.2f}{ratio}{cov}")
        lines.append(f"| {k} | " + " | ".join(cells) + " |")
    return "\n".join(lines)
