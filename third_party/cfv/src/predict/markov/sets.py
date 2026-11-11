"""Kümeler (people, chats): her öğe için "bugün var mı" ikili zinciri.

Bağlam: (dün var mı, önceki gün var mı, geçen hafta aynı gün var mı, son görülmeden beri
geçen gün kutusu). Öğe başına sayım, aynı bağlamın genel (tüm öğeler, haftanın günü dahil)
olasılığına Dirichlet ile yaslanır. Aday havuzu: son POOL_DAYS günde görülen öğeler.
Küme seçimi: olasılığa göre sıralı ilk k, k beklenen Jaccard'ı en büyükleyen değer.
"""

from __future__ import annotations

from datetime import date, timedelta

from .chain import Chain

REC = (1, 2, 3, 7, 14, 30)  # son görülme kutuları


def _rec_bin(gap: int) -> int:
    return next((j for j, r in enumerate(REC) if gap <= r), len(REC))


def choose(probs: dict[str, float]) -> list[str]:
    items = sorted(probs.items(), key=lambda x: -x[1])
    total = sum(p for _, p in items)
    best_k, best = 0, 1.0
    for _, p in items:  # k=0: hepsinin yok olma olasılığı
        best *= 1 - p
    s = 0.0
    for k, (_, p) in enumerate(items, 1):
        s += p
        ej = s / (k + total - s)
        if ej > best:
            best_k, best = k, ej
    return sorted(x for x, _ in items[:best_k])


class Sets:
    def __init__(self, by_day: dict[str, dict], key: str, alpha: float = 4.0, beta: float = 2.0,
                 half_life: float = 365.0, pool_days: int = 60):
        self.by_day, self.key, self.alpha, self.pool_days = by_day, key, alpha, pool_days
        self.glob = Chain(2, beta, half_life)
        self.per: dict[str, Chain] = {}
        self.half_life = half_life
        self.last: dict[str, str] = {}  # öğe → son görüldüğü gün

    def _has(self, day: date, n: int, x: str) -> bool:
        return x in self.by_day.get((day - timedelta(n)).isoformat(), {}).get(self.key, ())

    def _ctx(self, day: str, x: str) -> tuple:
        d = date.fromisoformat(day)
        gap = (d - date.fromisoformat(self.last[x])).days
        return (self._has(d, 1, x), self._has(d, 2, x), self._has(d, 7, x), _rec_bin(gap))

    def _pool(self, day: str) -> list[str]:
        d = date.fromisoformat(day)
        return [x for x, l in self.last.items() if (d - date.fromisoformat(l)).days <= self.pool_days]

    def probs(self, day: str, i: int) -> dict[str, float]:
        dow = date.fromisoformat(day).isoweekday()
        out = {}
        for x in self._pool(day):
            c = self._ctx(day, x)
            g = self.glob.prob([(), c, (c, dow)], i)
            ch = self.per.get(x)
            if ch is None:
                out[x] = g[1]
                continue
            row = ch.counts.get(c) or [0.0, 0.0, 0.0]
            w = ch._w(i)
            out[x] = (row[1] + self.alpha * w * g[1]) / (row[-1] + self.alpha * w)
        return out

    def predict(self, day: str, i: int) -> list[str]:
        return choose(self.probs(day, i))

    def sample(self, day: str, i: int, n: int, rng) -> list[list[str]]:
        """n örnek küme: her öğe kendi olasılığıyla bağımsız (öğeler arası ilişki yok)."""
        p = self.probs(day, i)
        return [sorted(x for x, q in p.items() if rng.random() < q) for _ in range(n)]

    def feed(self, day: str, i: int) -> None:
        r = self.by_day.get(day, {})
        if self.key not in r:
            return
        truth = set(r[self.key])
        dow = date.fromisoformat(day).isoweekday()
        for x in self._pool(day):
            c, y = self._ctx(day, x), int(x in truth)
            self.glob.add([(), c, (c, dow)], y, i)
            self.per.setdefault(x, Chain(2, 0.0, self.half_life)).add([c], y, i)
        for x in truth:
            self.last[x] = day
