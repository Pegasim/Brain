"""Saatlik durum zinciri (2. derece, saat + haftanın günü bağlamlı) ve gün toplamlarının örneklenmesi.

t gününü tahmin: t-1'in son iki saatinden başlayıp 24 saati N kez örnekle; her dilimde o
(durum, saat) için yakın geçmişteki emisyon vektörlerinden birini çek; gün toplamlarının
medyanı (MAE için en iyi nokta tahmini) tahmindir.

Varsayılanlar dev'de seçildi: gün toplamları için kısa bellek (yarı-ömür 7 gün, son 10 emisyon)
en iyisi; sonraki-saat doğruluğu ise uzun bellekle (β=16, yarı-ömür 365) en yüksek.
"""

from __future__ import annotations

import random
from bisect import bisect_right
from collections import deque
from datetime import date

from .chain import Chain
from .slots import EMIT, N_STATES


def contexts(p2: int, p1: int, h: int, dow: int, lvl: int | None = None) -> list[tuple]:
    we = dow >= 6
    c = [(), (h,), (p1, h), (p1, h, we), (p2, p1, h, we), (p2, p1, h, dow, "d")]
    return c if lvl is None else c + [(p1, h, lvl, "L"), (p2, p1, h, we, lvl, "L")]


class Hourly:
    def __init__(self, slots: dict, beta: float = 2.0, half_life: float = 7.0, pool: int = 10,
                 sims: int = 300, regimes: int = 0, edge_end: int | None = None):
        self.days, self.st, self.em = slots["days"], slots["states"], slots["emit"]
        self.chain = Chain(N_STATES, beta, half_life)
        # gün rejimi (gizli değişken): günün mesaj hacminin kantil kutusu, kendisi de bir zincir
        # (dünün rejimi + haftanın günü). Simülasyon başında çekilir, gün boyu sabit → örnek
        # günler arasında "yoğun gün / sakin gün" farkı oluşur (aksi hâlde dağılım fazla dar).
        self.R, self.level = regimes, {}
        if regimes:
            act = [sum(e[0] + e[1] for e in self.em[24 * i: 24 * i + 24]) for i in range(len(self.days))]
            ref = sorted(a for a in act[:edge_end] if a > 0)
            self.edges = [ref[len(ref) * q // regimes] for q in range(1, regimes)]
            self.act, self.lchain = act, Chain(regimes, beta, half_life)
        self.pools: dict[tuple[int, int], deque] = {}
        self.pool_n, self.sims = pool, sims
        self.hits = self.total = 0  # sonraki-dilim top-1 doğruluğu (gerçek geçmiş verilir)
        self.day_hits = 0  # gün başından (00:00) örneklenen dilim modu

    def _prev(self, i: int, h: int) -> tuple[int, int]:
        s = 24 * i + h
        return (self.st[s - 2] if s >= 2 else 0), (self.st[s - 1] if s >= 1 else 0)

    def _lctx(self, i: int, dow: int) -> list[tuple]:
        l1 = self.level.get(i - 1, self.R)  # R = bilinmiyor
        return [(), (dow,), (l1,), (l1, dow)]

    def feed(self, i: int) -> None:
        """i. günü öğren (tahminden SONRA çağrılır)."""
        dow = date.fromisoformat(self.days[i]).isoweekday()
        lvl = None
        if self.R:
            lvl = self.level[i] = bisect_right(self.edges, self.act[i])
            self.lchain.add(self._lctx(i, dow), lvl, i)
        for h in range(24):
            s = 24 * i + h
            p2, p1 = self._prev(i, h)
            self.chain.add(contexts(p2, p1, h, dow, lvl), self.st[s], i)
            for key in ((self.st[s], h), (self.st[s], h, lvl)):
                self.pools.setdefault(key, deque(maxlen=self.pool_n)).append(self.em[s])

    def score_next(self, i: int) -> None:
        """i. günün her dilimini gerçek önceki dilimlerle tahmin et (Π_max ile karşılaştırma için)."""
        dow = date.fromisoformat(self.days[i]).isoweekday()
        for h in range(24):
            p = self.chain.prob(contexts(*self._prev(i, h), h, dow), i)
            self.hits += max(range(N_STATES), key=p.__getitem__) == self.st[24 * i + h]
            self.total += 1

    def predict(self, i: int) -> dict:
        dow = date.fromisoformat(self.days[i]).isoweekday()
        rng = random.Random(i)
        zero = [0.0] * len(EMIT)
        p2, p1 = self._prev(i, 0)
        paths = [(p2, p1)] * self.sims
        lv = [None] * self.sims
        if self.R:
            lv = rng.choices(range(self.R), weights=self.lchain.prob(self._lctx(i, dow), i), k=self.sims)
        hours = [[] for _ in range(self.sims)]
        totals = [[0.0] * len(EMIT) for _ in range(self.sims)]
        occ = [[0] * N_STATES for _ in range(24)]
        states = range(N_STATES)
        for h in range(24):
            cache: dict[tuple, list[float]] = {}
            nxt = []
            for k, (a, b) in enumerate(paths):
                key = (a, b, lv[k])
                if key not in cache:
                    p = self.chain.prob(contexts(a, b, h, dow, lv[k]), i)
                    cache[key] = [sum(p[: j + 1]) for j in states]
                s = rng.choices(states, cum_weights=cache[key])[0]
                occ[h][s] += 1
                hours[k].append(s)
                pool = self.pools.get((s, h, lv[k])) or self.pools.get((s, h))
                e = rng.choice(pool) if pool else zero
                t = totals[k]
                for j, v in enumerate(e):
                    t[j] += v
                nxt.append((b, s))
            paths = nxt
        self.day_hits += sum(max(states, key=occ[h].__getitem__) == self.st[24 * i + h] for h in range(24))
        # örnek günler (world model'e ilk adım): 24 saatlik durum yolu + gün toplamları
        self.samples = [{"hours": "".join(chr(65 + x) for x in hs), **{k: round(t[j], 2) for j, k in enumerate(EMIT)}}
                        for hs, t in zip(hours, totals)]
        out = {}
        for j, k in enumerate(EMIT):
            vals = sorted(t[j] for t in totals)
            out[k] = round(vals[len(vals) // 2], 2)
        return out
