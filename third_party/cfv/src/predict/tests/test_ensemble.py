"""Birleşim yardımcıları — sentetik."""

import random

from ensemble.__main__ import blend, mix, simplex


def test_simplex():
    s = simplex(3)
    assert len(s) == 15 and all(abs(sum(w) - 1) < 1e-9 for w in s)


def test_blend_renormalizes_missing():
    ps = [{"x": 10}, {}, {"x": 20}]
    assert blend(ps, (0.5, 0.5, 0.0), "x") == 10  # eksik yaklaşımın payı ötekine geçer
    assert blend(ps, (0.0, 1.0, 0.0), "x") == 15  # pay sahibi yoksa eşit ortalama
    assert blend([{}, {}], (0.5, 0.5), "x") is None


def test_mix_proportions():
    out = mix([[{"x": 1}], [{"x": 2}]], (0.75, 0.25), 8, random.Random(0))
    assert sorted(o["x"] for o in out) == [1] * 6 + [2] * 2
