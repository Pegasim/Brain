"""Tahmin edilebilirlik tavanı (Song ve ark. 2010, "Limits of Predictability in Human Mobility").

S_real: Lempel-Ziv tahmincisi, S ≈ n·log2(n) / Σ Λ_i; Λ_i = i'de başlayıp [0, i) içinde hiç
görünmemiş en kısa alt dizinin uzunluğu. Fano: S = H(Π) + (1-Π)·log2(N-1) → Π_max
(N = görülen durum sayısı). Karşılaştırma için S_rand = log2 N ve S_unc (sıra bilgisi yok).
"""

from __future__ import annotations

import math
from collections import Counter


def lambdas(seq: str) -> list[int]:
    """Λ_i: i'de başlayıp seq[:i] içinde hiç görünmemiş en kısa alt dizinin uzunluğu."""
    out, L = [0], 1
    for i in range(1, len(seq)):
        L = max(1, L - 1)  # Λ_i ≥ Λ_{i-1} - 1
        while i + L <= len(seq) and seq[i:i + L] in seq[:i]:
            L += 1
        out.append(L)
    return out


def lz_entropy(lams: list[int], start: int = 0, end: int | None = None) -> float:
    """[start, end) dilimlerinin entropi oranı (bit/sembol); geçmiş = pencereden öncesi dahil hepsi.

    Kontoyiannis ve ark. (1998) biçimi: Ĥ = [ (1/n) Σ Λ_i / log2 i ]^-1 — pencere uzun bir
    geçmişin sonundaysa da tutarlı (Song'un n·log2 n / ΣΛ_i biçimi yalnız tüm dizi için).
    """
    idx = range(max(start, 1), len(lams) if end is None else end)
    return len(idx) / sum(lams[i] / math.log2(i + 1) for i in idx)


def shannon(seq: str) -> float:
    n = len(seq)
    return -sum(c / n * math.log2(c / n) for c in Counter(seq).values())


def fano(S: float, N: int) -> float:
    """S = H(Π) + (1-Π)log2(N-1) denklemini Π ∈ [1/N, 1] için çöz (sağ taraf Π'de azalan)."""
    if N < 2:
        return 1.0

    def f(p: float) -> float:
        h = 0.0 if p in (0.0, 1.0) else -p * math.log2(p) - (1 - p) * math.log2(1 - p)
        return h + (1 - p) * math.log2(N - 1)

    lo, hi = 1.0 / N, 1.0
    if S >= f(lo):
        return lo
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if f(mid) > S else (lo, mid)
    return (lo + hi) / 2


def report(seq: str, lams: list[int], start: int = 0, end: int | None = None) -> dict:
    """seq[start:end] penceresi için tavan (LZ geçmişi pencereden öncesini de görür)."""
    s_real = lz_entropy(lams, start, end)
    seq = seq[start:end]
    N = len(set(seq))
    s_unc = shannon(seq)
    return {"n": len(seq), "N": N, "S_rand": math.log2(N) if N else 0.0, "S_unc": s_unc,
            "S_real": s_real, "Pi_max": fano(s_real, N), "Pi_unc": fano(s_unc, N),
            "top1_marginal": max(Counter(seq).values()) / len(seq)}
