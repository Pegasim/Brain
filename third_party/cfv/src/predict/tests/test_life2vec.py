"""life2vec: t'nin tahmini t ve sonrasından etkilenmemeli — tamamen SENTETİK veri."""

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from common.targets import COUNTS  # noqa: E402
from life2vec.__main__ import pick_set  # noqa: E402
from life2vec.data import IN_COUNTS, Raw, count_recency, recency, tensors  # noqa: E402
from life2vec.model import Life2Vec, batch  # noqa: E402

N = 60


def _raw(seed=0) -> Raw:
    rng = np.random.default_rng(seed)
    days = [f"2025-01-{i:02d}" if i < 32 else f"2025-02-{i - 31:02d}" for i in range(1, N + 1)]
    y = rng.poisson(5, (N, len(COUNTS))).astype(np.float32)
    y[:10, -1] = np.nan  # eksik kaynak
    x = np.concatenate([np.log1p(rng.poisson(5, (N, len(IN_COUNTS)))), np.zeros((N, len(IN_COUNTS)))], 1)
    ctx = rng.normal(size=(N, 15)).astype(np.float32)
    sessions = [(i, f"c{rng.integers(5)}", [str(rng.integers(5))], list(rng.normal(size=7)))
                for i in range(N) for _ in range(rng.integers(1, 4))]
    sets = {"people": [[str(v) for v in rng.choice(5, 2)] for _ in range(N)],
            "chats": [[f"c{v}" for v in rng.choice(5, 2)] for _ in range(N)]}
    return Raw(days, y, x.astype(np.float32), ctx, count_recency(y), sessions, sets)


def test_recency_uses_only_past():
    a = np.random.default_rng(1).integers(0, 2, (30, 4)).astype(np.float32)
    r = recency(a, a, a)
    b = a.copy()
    b[20:] = 1 - b[20:]  # t=20 ve sonrası değişir
    r2 = recency(b, b, b)
    assert np.allclose(r[:21], r2[:21]) and not np.allclose(r[21:], r2[21:])
    y = a * 3
    z = y.copy()
    z[20:] += 7
    assert np.allclose(count_recency(y)[:21], count_recency(z)[:21])


def test_model_is_causal():
    raw = _raw()
    tn = tensors(raw, 30)
    torch.manual_seed(0)
    m = Life2Vec(raw, tn).eval()
    with torch.no_grad():
        mu, _, lg = m(batch(raw, tn, N, "cpu"))
        raw.x[30:] += 5  # t=30 ve sonrasının girdileri
        raw.sessions = [s if s[0] < 30 else (s[0], "c0", ["0"], [9.0] * 7) for s in raw.sessions]
        tn2 = tensors(raw, 30)
        mu2, _, lg2 = m(batch(raw, tn2, N, "cpu"))
    assert torch.allclose(mu[:31], mu2[:31]) and not torch.allclose(mu[31:], mu2[31:])
    assert torch.allclose(lg["people"][:31], lg2["people"][:31])


def test_pick_set():
    assert sorted(pick_set(np.array([0.9, 0.8, 0.05, 0.02]))) == [0, 1]
    assert len(pick_set(np.array([0.1, 0.1, 0.1]))) >= 1


def test_count_recency_falls_back_to_last_known():
    y = np.full((20, 1), np.nan, np.float32)
    y[3] = 9.0  # tek bilinen değer, sonra uzun boşluk
    r = count_recency(y)
    assert np.allclose(r[15, 0, [0, 1, 2, 3]], np.log1p(9.0))  # son bilinen, pencereler boş → son bilinen
    assert r[15, 0, 5] > r[5, 0, 5]  # yaş büyür
