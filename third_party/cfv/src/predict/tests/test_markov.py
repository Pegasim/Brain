"""markov yaklaşımı testleri — tamamen SENTETİK veri."""

import math
import random
from datetime import date, timedelta

from markov import ceiling
from markov.chain import Chain
from markov.daylevel import DayLevel, wmedian
from markov.hourly import Hourly
from markov.sets import Sets, choose
from markov.slots import EMIT


def test_chain_backoff():
    c = Chain(2, beta=1.0, half_life=None)
    for _ in range(50):
        c.add([(), ("a",)], 0, 0)
        c.add([(), ("b",)], 1, 0)
    assert c.prob([(), ("a",)], 0)[0] > 0.95
    assert c.prob([(), ("b",)], 0)[1] > 0.95
    assert abs(c.prob([(), ("yok",)], 0)[0] - 0.5) < 0.02  # görülmemiş bağlam → genel


def test_chain_forgets():
    c = Chain(2, beta=0.1, half_life=1.0)
    for d in range(10):
        c.add([()], 0, d)
    for d in range(10, 20):
        c.add([()], 1, d)
    assert c.prob([()], 20)[1] > 0.9  # eski günler sönmüş


def test_ceiling():
    periodic = "ABCD" * 500
    assert ceiling.report(periodic, ceiling.lambdas(periodic))["Pi_max"] > 0.9
    rng = random.Random(0)
    noise = "".join(rng.choice("ABCD") for _ in range(2000))
    r = ceiling.report(noise, ceiling.lambdas(noise))
    # LZ kısa dizide entropiyi düşük tahmin eder (bilinen sapma) → Π_max gürültüde de 1/N'den büyük
    assert r["Pi_max"] < 0.6 and abs(r["S_unc"] - 2) < 0.05
    assert math.isclose(ceiling.fano(2.0, 4), 0.25, abs_tol=1e-6)


def test_choose():
    assert choose({"a": 0.9, "b": 0.8, "c": 0.05}) == ["a", "b"]
    assert choose({"a": 0.05, "b": 0.02}) == []


def test_wmedian():
    assert wmedian([(1, 1.0), (5, 1.0), (9, 3.0)]) == 9
    assert wmedian([(1, 2.0), (5, 1.0), (9, 0.5)]) == 1


def _days(n):
    d0 = date(2030, 1, 1)
    return [(d0 + timedelta(i)).isoformat() for i in range(n)]


def test_daylevel_weekly_pattern():
    days = _days(200)
    by_day = {d: {"x": 10 if date.fromisoformat(d).isoweekday() == 1 else 0} for d in days}
    m = DayLevel(by_day, "x", days[150])
    for i, d in enumerate(days[:150]):
        m.feed(d, i)
    preds = {d: m.predict(d, 150 + j) for j, d in enumerate(days[150:157])}
    assert all(v == by_day[d]["x"] for d, v in preds.items())


def test_sets_persistent_and_weekly():
    days = _days(120)
    by_day = {d: {"people": ["1"] + (["2"] if i % 7 == 0 else [])} for i, d in enumerate(days)}
    m = Sets(by_day, "people")
    for i, d in enumerate(days[:105]):
        m.feed(d, i)
    assert m.predict(days[105], 105) == ["1", "2"]  # "2" haftada bir, geçen hafta aynı gün vardı
    m.feed(days[105], 105)
    assert m.predict(days[106], 106) == ["1"]


def test_hourly_daily_routine():
    days = _days(60)
    states, emit = [], []
    for _ in days:  # her gün 9-17 benden mesaj (durum 2), saat başı 3 mesaj
        for h in range(24):
            on = 9 <= h < 17
            states.append(2 if on else 0)
            e = [0.0] * len(EMIT)
            e[0] = 3.0 if on else 0.0
            emit.append(e)
    sl = {"days": days, "states": states, "emit": emit}
    m = Hourly(sl, sims=50)
    for i in range(50):
        m.feed(i)
    assert m.predict(50)["msgs_sent"] == 24.0
    assert len(m.samples) == 50 and all(s["hours"] == "A" * 9 + "C" * 8 + "A" * 7 for s in m.samples)
    m.score_next(50)
    assert m.hits == 24
    r = Hourly(sl, sims=20, regimes=2, edge_end=40)  # gün rejimi açıkken de aynı rutin
    for i in range(50):
        r.feed(i)
    assert r.predict(50)["msgs_sent"] == 24.0


def test_sampling():
    rng = random.Random(0)
    days = _days(60)
    by_day = {d: {"x": 5, "people": ["1"]} for d in days}
    dl = DayLevel(by_day, "x", days[50])
    s = Sets(by_day, "people")
    for i, d in enumerate(days[:50]):
        dl.feed(d, i)
        s.feed(d, i)
    assert dl.sample(days[50], 50, 10, rng) == [5] * 10
    assert s.sample(days[50], 50, 10, rng) == [["1"]] * 10
