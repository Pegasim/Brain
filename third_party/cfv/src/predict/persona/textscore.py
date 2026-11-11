"""Metin değerlendirme: tahmini gün raporu ↔ gerçek gün raporu (report.md biçimi).

- frontmatter: projects / people Jaccard (adlar küçük harfe katlanır)
- olaylar: 'Neler oldu' maddeleri yerel embedding'le eşlenir; eşik üstü yumuşak P/R/F1
  (her gerçek madde en benzer tahmin maddesiyle; BERTScore'un madde düzeyi hâli)
- özet: başlık+özet cosine
Embedding: bge-m3, CPU'da (sentence-transformers; GPU'da model yok).
"""

from __future__ import annotations

import math
import re
from functools import cache

EMBED = "BAAI/bge-m3"
THRESH = 0.6


def parse(md: str) -> dict:
    fm, body = {}, md
    if m := re.match(r"\s*---\n(.*?)\n---\n(.*)", md, re.S):
        body = m.group(2)
        for k, v in re.findall(r"^(\w+):\s*\[(.*)\]\s*$", m.group(1), re.M):
            fm[k] = {x.strip().strip("'\"").casefold() for x in v.split(",") if x.strip()}
    title = (re.search(r"^# .*?—\s*(.*)$", body, re.M) or re.search(r"^# (.*)$", body, re.M))
    summary = body.split("\n## ", 1)[0]
    summary = re.sub(r"^# .*$", "", summary, flags=re.M).strip()
    ev = re.search(r"^## Neler oldu\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    bullets = [re.sub(r"\s*[\[(][^\])]*[\])]\s*$", "", b).strip()
               for b in re.findall(r"^- (.+)$", ev.group(1), re.M)] if ev else []
    return {**fm, "title": title.group(1).strip() if title else "", "summary": summary, "bullets": bullets}


def jaccard(a: set, b: set) -> float:
    return 1.0 if not a and not b else len(a & b) / len(a | b)


@cache
def _model():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(EMBED, device="cpu")


def embed(texts: list[str]) -> list[list[float]]:
    return _model().encode(texts, batch_size=8, normalize_embeddings=True).tolist()


def cos(a, b) -> float:
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a) * sum(y * y for y in b)) or 1)


def soft_f1(sim: list[list[float]], thresh: float = THRESH) -> tuple[float, float, float]:
    """sim[i][j] = tahmin i ↔ gerçek j. P: eşleşen tahmin oranı, R: karşılanan gerçek oranı."""
    if not sim or not sim[0]:
        return 0.0, 0.0, 0.0
    p = sum(max(r) >= thresh for r in sim) / len(sim)
    r = sum(max(col) >= thresh for col in zip(*sim)) / len(sim[0])
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def score(pred_md: str, true_md: str, use_embed: bool = True) -> dict:
    p, t = parse(pred_md), parse(true_md)
    out = {k: jaccard(p.get(k, set()), t.get(k, set())) for k in ("projects", "people", "tags")}
    if use_embed and p["bullets"] and t["bullets"]:
        e = embed(p["bullets"] + t["bullets"] + [p["title"] + ". " + p["summary"], t["title"] + ". " + t["summary"]])
        pe, te = e[:len(p["bullets"])], e[len(p["bullets"]):-2]
        out["ev_p"], out["ev_r"], out["ev_f1"] = soft_f1([[cos(a, b) for b in te] for a in pe])
        out["summary_cos"] = cos(e[-2], e[-1])
    return out


def gold(day: str) -> str | None:
    from common import data_dir
    p = data_dir() / "processed" / "days" / day[:4] / f"{day}.md"
    return p.read_text() if p.exists() else None


def mean(rows: list[dict]) -> dict:
    keys = sorted({k for r in rows for k in r})
    return {k: round(sum(r[k] for r in rows if k in r) / sum(k in r for r in rows), 3) for k in keys} | {"n": len(rows)}


def doc(md: str) -> str:
    p = parse(md)
    return (p["title"] + ". " + p["summary"] + " " + " ".join(p["bullets"]))[:4000]


def discrimination(preds: dict[str, str], golds: dict[str, str]) -> float:
    """Üsluptan bağımsız: tahmin_t, gerçek_t'ye öteki günlerin gerçeklerinden ne sıklıkla daha yakın
    (t-1 ve t-7 havuz dışı: rakiplerin kaynağı). 0.5 = şans, 1 = hep doğru gün."""
    ds = sorted(golds)
    g = dict(zip(ds, embed([doc(golds[d]) for d in ds])))
    wins = []
    for d, p in preds.items():
        pe = embed([doc(p)])[0]
        own = cos(pe, g[d])
        skip = {d, _prev(d, 1), _prev(d, 7)}
        wins += [own > cos(pe, g[o]) for o in ds if o not in skip]
    return round(sum(wins) / len(wins), 3) if wins else float("nan")


def _prev(d: str, n: int) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(d) - timedelta(n)).isoformat()


def compare(days: list[str], pred) -> dict:
    """pred(gün) → tahmini rapor metni (ya da None). Gerçek raporu olan günlerde ortalama skor."""
    pairs = {d: (p, g) for d in days if (g := gold(d)) and (p := pred(d))}
    if not pairs:
        return {}
    golds = {d: g for d in days if (g := gold(d))}
    return mean([score(p, g) for p, g in pairs.values()]) | {
        "ayırt": discrimination({d: p for d, (p, _) in pairs.items()}, golds)}
