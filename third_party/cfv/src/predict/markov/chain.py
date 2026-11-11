"""Bağlamlı Markov zinciri: Dirichlet backoff + zamanla unutma.

P(s | c_k) = (n(c_k, s) + β·P(s | c_{k-1})) / (n(c_k) + β), c_0 = genel, c_K = en özel bağlam.
Seyrek bağlamda sayım azsa olasılık kendiliğinden genel bağlama yaslanır (backoff).
Unutma: gün g'de eklenen sayım 2^(g/H) ağırlıkla yazılır; β de aynı ölçekle büyütülür →
eski günler yarı-ömür H ile söner, sayımları her gün çarpmaya gerek kalmaz.
"""

from __future__ import annotations

from collections import defaultdict


class Chain:
    def __init__(self, n: int, beta: float = 2.0, half_life: float | None = 90.0):
        self.n, self.beta, self.H = n, beta, half_life
        self.counts: dict[tuple, list[float]] = defaultdict(lambda: [0.0] * (n + 1))  # son eleman: toplam

    def _w(self, day: int) -> float:
        return 2.0 ** (day / self.H) if self.H else 1.0

    def add(self, ctxs: list[tuple], s: int, day: int) -> None:
        w = self._w(day)
        for c in ctxs:
            row = self.counts[c]
            row[s] += w
            row[-1] += w

    def prob(self, ctxs: list[tuple], day: int) -> list[float]:
        """ctxs genelden özele; day: tahmin günü (β ölçeği için)."""
        beta = self.beta * self._w(day)
        p = [1.0 / self.n] * self.n
        for c in ctxs:
            row = self.counts.get(c)
            if row and row[-1] > 0:
                tot = row[-1] + beta
                p = [(row[s] + beta * p[s]) / tot for s in range(self.n)]
        return p
