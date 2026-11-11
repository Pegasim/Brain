"""Walk-forward: her ay başında yalnız < ay verisiyle eğit, ayın günlerini tahmin et.

    flock ../../data/processed/predict/gpu.lock uv run --group life2vec python -m life2vec --split dev test
"""

from __future__ import annotations

import argparse
import time
from datetime import date

import numpy as np
import torch

from common import SPLITS, out_dir
from common.evaluate import write_preds
from common.targets import COUNTS

from .data import CONT, SCALE, SET_KEYS, Raw, load, tensors
from .model import Life2Vec, batch, loss

VAL_DAYS = 42


def pick_set(p: np.ndarray) -> np.ndarray:
    """Beklenen Jaccard'ı (yaklaşık) en büyük yapan top-k: E[J] ≈ Σ_{i≤k} p_i / (k + Σ_{i>k} p_i)."""
    o = np.argsort(-p)
    cs = np.cumsum(p[o])
    k = int(np.argmax(cs / (np.arange(1, len(p) + 1) + p.sum() - cs))) + 1
    return o[:k]


def train(raw: Raw, cut: int, seed: int, device, max_ep: int = 1000, patience: int = 100):
    """Eğitim t < cut-VAL_DAYS, erken durma [cut-VAL_DAYS, cut). Hiçbir şey ≥ cut görmez."""
    torch.manual_seed(seed)
    tn = tensors(raw, cut)
    b = batch(raw, tn, cut, device)
    m = Life2Vec(raw, tn).to(device)
    tr = torch.zeros(cut, dtype=torch.bool, device=device)
    va = tr.clone()
    tr[: cut - VAL_DAYS] = True
    va[cut - VAL_DAYS:] = True
    opt = torch.optim.AdamW(m.parameters(), lr=3e-3, weight_decay=1e-2)
    best, state, since = float("inf"), None, 0
    for ep in range(max_ep):
        m.train()
        opt.zero_grad()
        loss(m, m(b), b, tr).backward()
        torch.nn.utils.clip_grad_norm_(m.parameters(), 1.0)
        opt.step()
        if ep % 5:
            continue
        m.eval()
        with torch.no_grad():
            vl = loss(m, m(b), b, va).item()
        if vl < best:
            best, state, since = vl, {k: v.clone() for k, v in m.state_dict().items()}, 0
        elif (since := since + 5) >= patience:
            break
    m.load_state_dict(state)
    m.eval()
    return m, tn, {"val": round(best, 3), "ep": ep}


def predict_block(raw: Raw, cut: int, end: int, seeds: int, device, n_samples: int = 100):
    """Ay bloğu [cut, end): seed başına eğit; sayılar = seed'lerin NB karışımı (nokta = medyan, MAE için en iyi)."""
    rng = np.random.default_rng(cut)
    draws, probs, info = [], {k: [] for k in SET_KEYS}, []
    for s in range(seeds):
        m, tn, inf = train(raw, cut, s, device)
        info.append(inf)
        with torch.no_grad():
            log_mu, log_r, logits = m(batch(raw, tn, end, device))
        mu = log_mu[cut:end].exp().cpu().numpy()
        r = log_r[cut:end].exp().cpu().numpy()
        size = (2000 // seeds, *mu.shape)
        cap = 2 * np.nanmax(raw.y[:cut], 0)  # log ölçeğinde Laplace kuyruğu → geçmiş en büyüğün 2 katında kes
        lap = np.expm1(np.log(mu) + rng.laplace(0, r, size)).clip(0, cap)
        draws.append(np.where(CONT, lap, rng.negative_binomial(r, r / (r + mu), size)))
        for k in SET_KEYS:
            probs[k].append(torch.sigmoid(logits[k][cut:end]).cpu().numpy())
    draws = np.concatenate(draws) / np.array([SCALE.get(k, 1) for k in COUNTS])  # [S, gün, C]
    med = np.median(draws, 0)
    rows, samples = [], []
    for j, d in enumerate(raw.days[cut:end]):
        row = {"day": d} | {k: round(float(med[j, c]), 2) for c, k in enumerate(COUNTS)}
        for k in SET_KEYS:
            p = np.mean([pp[j] for pp in probs[k]], 0)
            row[k] = sorted(tn.vocab[k][i] for i in pick_set(p))
        rows.append(row)
        pick = rng.choice(len(draws), n_samples, replace=False)
        samples.append({"day": d, "samples": [{k: float(draws[i, j, c]) for c, k in enumerate(COUNTS)} for i in pick]})
    return rows, samples, info


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m life2vec")
    ap.add_argument("--split", choices=list(SPLITS), nargs="+", default=["dev"])
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--every", choices=["week", "month"], default="week", help="yeniden eğitim sıklığı")
    a = ap.parse_args()
    torch.set_num_threads(6)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    raw = load()
    key = (lambda d: d[:7]) if a.every == "month" else (lambda d: "%d-W%02d" % date.fromisoformat(d).isocalendar()[:2])
    for split in a.split:
        lo, hi = SPLITS[split]
        hi = hi or raw.days[-1]
        rows, samples = [], []
        for blk in sorted({key(d) for d in raw.days if lo <= d <= hi}):
            t0 = time.time()
            idx = [i for i, d in enumerate(raw.days) if key(d) == blk and lo <= d <= hi]
            out, smp, info = predict_block(raw, idx[0], idx[-1] + 1, a.seeds, device)
            rows += out
            samples += smp
            print(f"{split} {blk}: {len(out)} gün, {info}, {time.time() - t0:.0f}s", flush=True)
        write_preds(out_dir("life2vec") / f"pred_{split}.jsonl", rows)
        write_preds(out_dir("life2vec") / f"samples_{split}.jsonl", samples)


if __name__ == "__main__":
    main()
