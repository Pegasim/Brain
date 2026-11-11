"""uv run --group persona python -m persona jobs memo --first G --last G [--agents 5]  # mini-rapor işleri
uv run --group persona python -m persona jobs day [--split test] [--k 3] [--agents 5]  # 'günü hayal et' işleri
    → persona/{memo,imagined}/h<i>.txt listelerini o klasörde açılan Haiku ajanları işler; GPU'da LLM yok
uv run --group persona python -m persona predict [--split test]   # hatırla + topla → pred/samples
uv run --group persona python -m persona memoscore|textscore [--first G --last G]   # gerçek gün raporlarıyla
Embedding CPU'da; ağır adımlar gpu.lock altında (aynı anda tek ağır iş)."""

from __future__ import annotations

import argparse
import fcntl
import json
from contextlib import contextmanager
from datetime import date, timedelta
from functools import cache

from common import SPLITS, cfv_db, data, out_dir, ro
from common.evaluate import split_days, write_preds
from common.targets import days_between, known

from . import jobs, memo, predict, textscore


@contextmanager
def heavy():
    with open(out_dir() / "gpu.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _prev(d: str, n: int) -> str:
    return (date.fromisoformat(d) - timedelta(n)).isoformat()


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m persona")
    ap.add_argument("cmd", choices=["jobs", "predict", "memoscore", "textscore"])
    ap.add_argument("kind", nargs="?", choices=["memo", "day"], help="jobs için iş türü")
    ap.add_argument("--first")
    ap.add_argument("--last")
    ap.add_argument("--split", choices=list(SPLITS), default="test")
    ap.add_argument("--k", type=int, default=3, help="gün başına olası rapor sayısı")
    ap.add_argument("--agents", type=int, default=5, help="iş listesi sayısı (Haiku ajanı başına bir)")
    ap.add_argument("--name", default="persona", help="çıktı klasörü")
    a = ap.parse_args()
    by_day = data.load_days()
    in_range = lambda d: (not a.first or d >= a.first) and (not a.last or d <= a.last)

    if a.cmd == "jobs" and a.kind == "memo":
        jd = out_dir(f"{a.name}/memo")
        todo = [jobs.write(jd, d, memo.prompt(d)) for d in days_between(a.first, a.last) if not memo.path(d, a.name).exists()]
        print(len(todo), "mini-rapor işi:", *jobs.assign(jd, todo, a.agents))

    elif a.cmd == "jobs":
        names, jd = predict.name_map(), out_dir(f"{a.name}/imagined")
        todo = [jobs.write(jd, d, predict.prompt({x: r for x, r in by_day.items() if x < d}, d, a.k, names))
                for d in split_days(a.split, by_day) if in_range(d) and not predict.imagined(d, a.name).exists()]
        print(len(todo), "hayal işi:", *jobs.assign(jd, todo, a.agents))

    elif a.cmd == "predict":
        days = [d for d in split_days(a.split, by_day) if in_range(d)]
        with ro(cfv_db()) as conn, heavy():  # hatırlama CPU'da embedding ister
            kcal = cache(lambda d: predict.known_cal(known(conn, d)))
            out = [(d, r) for d in days if (r := predict.run(by_day, d, kcal, a.name))]
        write_preds(out_dir(a.name) / f"pred_{a.split}.jsonl", [r for _, (r, _) in out])
        write_preds(out_dir(a.name) / f"samples_{a.split}.jsonl",  # olasılıksal ölçüm (CRPS)
                    [{"day": d, "samples": ss} for d, (_, ss) in out])
        print(f"{len(out)}/{len(days)} gün → {a.name}/pred_{a.split}.jsonl")

    else:  # gerçek gün raporlarıyla metin skoru
        days = days_between(a.first or "2026-10-01", a.last or "2026-10-31")
        def read(p):
            return p.exists() and p.read_text()
        def first_sample(d):
            p = out_dir(f"{a.name}/samples") / f"{d}.json"
            return json.loads(p.read_text())["reports"][0] if p.exists() else None
        rivals = {"dünün raporu": lambda d: textscore.gold(_prev(d, 1)),
                  "geçen hafta aynı gün": lambda d: textscore.gold(_prev(d, 7))}
        cands = {"mini-rapor haiku": lambda d: read(memo.path(d)),
                 "mini-rapor gemma": lambda d: read(memo.path(d, "persona_gemma"))} \
            if a.cmd == "memoscore" else {f"{a.name} (ilk örnek)": first_sample}
        with heavy():
            for label, fn in (cands | rivals).items():
                print(label, textscore.compare(days, fn), flush=True)


if __name__ == "__main__":
    main()
