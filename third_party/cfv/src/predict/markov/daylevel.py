"""Gün düzeyinde zincir: her sayı hedefi kantil kutularına bölünür, kutu dizisi Markov.

Bağlam (genelden özele): genel → haftanın günü → (dünün kutusu, gün) → (dün, geçen haftanın
aynı günü, gün). Tahmin = kutu olasılıklarıyla ağırlıklı, kutulardaki yakın değerlerin
ağırlıklı medyanı (kutu başına son `pool` değer: dev'de küçük havuz = yakın geçmiş daha iyi).
Kutu sınırları ilk tahmin gününden önceki veriden bir kez çıkarılır.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import deque
from datetime import date, timedelta

from .chain import Chain


def edges(values: list[float], k: int = 5) -> list[float]:
    v = sorted(values)
    return sorted({v[int(len(v) * q / k)] for q in range(1, k)}) if v else []


def wmedian(pairs: list[tuple[float, float]]) -> float:
    pairs.sort()
    half, acc = sum(w for _, w in pairs) / 2, 0.0
    for x, w in pairs:
        acc += w
        if acc >= half:
            return x
    return pairs[-1][0]


class DayLevel:
    def __init__(self, by_day: dict[str, dict], key: str, before: str, k: int = 8,
                 beta: float = 4.0, half_life: float = 120.0, pool: int = 10):
        self.by_day, self.key = by_day, key
        self.edges = edges([r[key] for d, r in by_day.items() if d < before and key in r], k)
        self.nb = len(self.edges) + 1
        self.chain = Chain(self.nb, beta, half_life)
        self.pools = [deque(maxlen=pool) for _ in range(self.nb)]

    def _bin(self, day: str) -> int:
        v = self.by_day.get(day, {}).get(self.key)
        return self.nb if v is None else bisect_right(self.edges, v)  # nb = bilinmiyor

    def _ctx(self, day: str) -> list[tuple]:
        d = date.fromisoformat(day)
        dow = d.isoweekday()
        b1 = self._bin((d - timedelta(1)).isoformat())
        b7 = self._bin((d - timedelta(7)).isoformat())
        return [(), (dow,), (b1, dow), (b1, b7, dow)]

    def feed(self, day: str, i: int) -> None:
        v = self.by_day.get(day, {}).get(self.key)
        if v is None:
            return
        b = bisect_right(self.edges, v)
        self.chain.add(self._ctx(day), b, i)
        self.pools[b].append(v)

    def sample(self, day: str, i: int, n: int, rng) -> list[float]:
        """Tahmin dağılımından n örnek: kutu ~ zincir, değer ~ kutunun yakın değerleri."""
        p = self.chain.prob(self._ctx(day), i)
        bins = [b for b, pool in enumerate(self.pools) if pool]
        if not bins:
            return []
        return [rng.choice(self.pools[b]) for b in rng.choices(bins, weights=[p[b] for b in bins], k=n)]

    def predict(self, day: str, i: int) -> float | None:
        p = self.chain.prob(self._ctx(day), i)
        pairs = [(v, p[b] / len(pool)) for b, pool in enumerate(self.pools) if pool for v in pool]
        return round(wmedian(pairs), 2) if pairs else None
