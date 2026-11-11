"""uv run python -m common build|baselines|eval [yaklaşım...] [--split dev|test] [--on yaklaşım]"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import SPLITS, out_dir
from . import data
from .baselines import BASELINES, SAMPLERS
from .evaluate import evaluate, read_preds, split_days, table, write_preds


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m common")
    ap.add_argument("cmd", choices=["build", "baselines", "eval", "all"])
    ap.add_argument("approaches", nargs="*")
    ap.add_argument("--split", choices=list(SPLITS), default="dev")
    ap.add_argument("--on", help="yalnız bu yaklaşımın tahmin ettiği günlerde ölç (ön deneme/alt küme)")
    ap.add_argument("--last", help="son gün (varsayılan: en yeni WhatsApp gününden bir önceki)")
    a = ap.parse_args()

    if a.cmd in ("build", "all"):
        print(data.build(last=a.last))
    if a.cmd in ("baselines", "all"):
        by_day = data.load_days()
        for split in SPLITS:
            days = split_days(split, by_day)
            for name, fn in BASELINES.items():
                hist = [data.history(by_day, d) for d in days]
                write_preds(out_dir(name) / f"pred_{split}.jsonl", [fn(h, d) for h, d in zip(hist, days)])
                write_preds(out_dir(name) / f"samples_{split}.jsonl", [SAMPLERS[name](h, d) for h, d in zip(hist, days)])
            print(f"{split}: {days[0]}..{days[-1]} ({len(days)} gün) rakipler yazıldı")
    if a.cmd in ("eval", "all"):
        by_day = data.load_days()
        names = a.approaches or sorted(
            p.parent.name for p in out_dir().glob(f"*/pred_{a.split}.jsonl"))
        names = list(BASELINES) + [n for n in names if n not in BASELINES]
        days, tag = None, ""
        if a.on:
            days = sorted(d for d in read_preds(out_dir(a.on) / f"pred_{a.split}.jsonl") if d in by_day)
            tag = f"_on_{a.on}"
        res = [evaluate(n, a.split, by_day, days) for n in names]
        span = f"{res[0]['days'][0]}..{res[0]['days'][1]}" + (f", {len(days)} gün ({a.on} günleri)" if a.on else "")
        md = (f"# {a.split} ({span})\n\nSayılar: MAE (en iyi rakibe oran); kümeler: Jaccard\n\n{table(res)}\n\n"
              f"## Olasılıksal: CRPS (en iyi rakibe oran) [%10-90 kapsaması, ideal 0.80]\n\n{table(res, 'crps')}\n")
        Path(out_dir() / f"leaderboard_{a.split}{tag}.md").write_text(md)
        print(md)


if __name__ == "__main__":
    main()
