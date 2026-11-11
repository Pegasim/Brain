"""Gün dizisi: t gününün tahmini yalnız t'den önceki girdileri ve t başında bilinen bağlamı görür.

İndeks t = gün. Girdi x[i] gün i bitince bilinir; GRU durumu h[t-1] t'yi tahmin eder.
Bağlam ctx[t]: haftanın günü, yılın günü, t'ye önceden girilmiş takvim (created < t 00:00).
Tazelik R[t, v]: kişi/sohbet v için yalnız < t günlerinden (dün, 7/28 gün oranı, aynı gün, son görülme).
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from common import cfv_db, common_db, ro
from common.data import load_days
from common.targets import COUNTS

SET_KEYS = ["people", "chats"]
CONT = [k in ("steps", "sleep_min") for k in COUNTS]  # aykırı değerli sürekli hedefler → log-Laplace
SCALE = {"cal_hours": 4}  # negatif binom tamsayı ister: saat → çeyrek saat
# Ödev 'done' bayrağı notun son hâli (t'den sonra işaretlenmiş olabilir) → girdide yalnız toplam
IN_COUNTS = [k for k in COUNTS if not k.startswith("todo_")] + ["todo_total"]
N_REC = 11
N_CREC = 6
MIN_DAYS = 1  # sözlüğe girmek için hedef kümede en az bu kadar gün
MIN_SESS = 2  # ya da en az bu kadar oturumda görünmek


@dataclass
class Raw:
    days: list[str]
    y: np.ndarray        # [N, C] hedef sayılar (ölçekli), NaN = yok
    x: np.ndarray        # [N, 2*len(IN_COUNTS)] log1p sayılar + eksik bayrağı
    ctx: np.ndarray      # [N, K] t başında bilinen
    crec: np.ndarray     # [N, C, N_CREC] sayı hedeflerinin tazeliği (yalnız < t)
    sessions: list[tuple]  # (gün_idx, chat_jid, [kişi], özellik)
    sets: dict[str, list]  # anahtar → gün başına liste ya da None


def _cyc(v: float, period: float) -> list[float]:
    a = 2 * math.pi * v / period
    return [math.sin(a), math.cos(a)]


def known_calendar(conn: sqlite3.Connection) -> dict[str, list[float]]:
    """gün → t başında bilinen takvim özeti (`common.targets.known` ile aynı kural: created < t 00:00):
    log1p(saatli olay), log1p(saat), ilk başlangıç/24, son bitiş/24, sabah/öğle/akşam saatleri/6, log1p(tüm gün)."""
    q = """SELECT local_date, start_utc, end_utc, coalesce(json_extract(payload,'$.all_day'),0) FROM event
           WHERE kind='calendar'
             AND json_extract(payload,'$.created') < strftime('%Y-%m-%dT%H:%M:%SZ', local_date, '-3 hours')"""
    ev: dict[str, list] = {}
    for d, s, e, allday in conn.execute(q):
        ev.setdefault(d, []).append((s, e, allday))
    out = {}
    for d, items in ev.items():
        timed, n_all, bins = [], 0, [0.0, 0.0, 0.0]
        for s, e, allday in items:
            if allday or not e:
                n_all += bool(allday)
                continue
            h0 = _local_hour(s)
            h1 = min(24.0, h0 + max(0.0, (_dt(e) - _dt(s)).total_seconds() / 3600))
            timed.append((h0, h1))
            for j, (lo, hi) in enumerate([(0, 12), (12, 18), (18, 24)]):
                bins[j] += max(0.0, min(h1, hi) - max(h0, lo))
        hours = min(24.0, sum(h1 - h0 for h0, h1 in timed))
        out[d] = [math.log1p(len(timed)), math.log1p(hours),
                  min((h0 for h0, _ in timed), default=0) / 24, max((h1 for _, h1 in timed), default=0) / 24,
                  *(min(b, 12) / 6 for b in bins), math.log1p(n_all)]
    return out


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _local_hour(iso: str) -> float:
    t = _dt(iso) + timedelta(hours=3)
    return t.hour + t.minute / 60


def load(common: Path | None = None, cfv: Path | None = None) -> Raw:
    by_day = load_days(common or common_db())
    days = sorted(by_day)
    idx = {d: i for i, d in enumerate(days)}
    with ro(cfv or cfv_db()) as conn:
        cal = known_calendar(conn)

    y = np.full((len(days), len(COUNTS)), np.nan, dtype=np.float32)
    x = np.zeros((len(days), 2 * len(IN_COUNTS)), dtype=np.float32)
    ctx = []
    for i, d in enumerate(days):
        r = by_day[d]
        for j, k in enumerate(COUNTS):
            if k in r:
                y[i, j] = round(r[k] * SCALE.get(k, 1))
        vals = {k: r.get(k) for k in IN_COUNTS}
        if "todo_done" in r:
            vals["todo_total"] = r["todo_done"] + r["todo_open"]
        for j, k in enumerate(IN_COUNTS):
            if vals[k] is None:
                x[i, len(IN_COUNTS) + j] = 1.0
            else:
                x[i, j] = math.log1p(vals[k])
        dt = date.fromisoformat(d)
        ctx.append([float(dt.isoweekday() == w) for w in range(1, 8)] + cal.get(d, [0.0] * 8))

    sessions = []
    with ro(common or common_db()) as conn:
        for day, jid, ctype, start, end, n, me, people in conn.execute(
                "SELECT day, chat_jid, chat_type, start_utc, end_utc, n_msgs, n_me, people FROM session ORDER BY day, start_utc"):
            if day not in idx:
                continue
            t0, t1 = _dt(start), _dt(end)
            f = _cyc(_local_hour(start), 24) + [math.log1p(n), math.log1p(me), float(me > 0),
                                  math.log1p((t1 - t0).total_seconds() / 60), float(ctype == "dm")]
            sessions.append((idx[day], jid, [str(p) for p in json.loads(people)], f))

    sets = {k: [by_day[d].get(k) for d in days] for k in SET_KEYS}
    hist = y.copy()  # ödevlerde geçmiş de yalnız toplam (done bayrağı sızar)
    tot = y[:, COUNTS.index("todo_done")] + y[:, COUNTS.index("todo_open")]
    hist[:, COUNTS.index("todo_done")] = hist[:, COUNTS.index("todo_open")] = tot
    return Raw(days, y, x, np.array(ctx, dtype=np.float32), count_recency(hist), sessions, sets)


def count_recency(y: np.ndarray) -> np.ndarray:
    """y[t, c] (NaN = yok) → [N, C, N_CREC]: son bilinen değer, 7/28 gün ve son 4 aynı gün ortalaması (log1p;
    pencerede değer yoksa son bilinen), 28 günde dolu oranı, son bilinenin yaşı. Yalnız y[<t]."""
    n, c = y.shape
    have = ~np.isnan(y)
    val = np.nan_to_num(y)
    cs_v = np.concatenate([np.zeros((1, c)), np.cumsum(val, 0)])
    cs_h = np.concatenate([np.zeros((1, c)), np.cumsum(have, 0)])
    t = np.arange(n)

    def mean(w):
        a = np.maximum(t - w, 0)
        s, k = cs_v[t] - cs_v[a], cs_h[t] - cs_h[a]
        return np.where(k > 0, s / np.maximum(k, 1), np.nan), k / w

    last, age = np.zeros((n, c)), np.full((n, c), 400.0)  # t'den önceki son bilinen değer ve yaşı (gün)
    for i in range(1, n):
        last[i] = np.where(have[i - 1], val[i - 1], last[i - 1])
        age[i] = np.where(have[i - 1], 1, np.minimum(age[i - 1] + 1, 400))
    m7, _ = mean(7)
    m28, f28 = mean(28)
    lag = np.stack([np.concatenate([np.full((7 * k, c), np.nan), y[:-7 * k]])[:n] for k in range(1, 5)])
    k4 = (~np.isnan(lag)).sum(0)
    dow = np.where(k4 > 0, np.nansum(lag, 0) / np.maximum(k4, 1), np.nan)
    m7, m28, dow = (np.where(np.isnan(v), last, v) for v in (m7, m28, dow))
    return np.stack([np.log1p(last), np.log1p(m7), np.log1p(m28), np.log1p(dow), f28, np.log1p(age) / 6],
                    -1).astype(np.float32)


@dataclass
class Tensors:
    vocab: dict[str, list[str]]  # anahtar → öğeler (indeks 1..V; 0 = bilinmeyen)
    s_day: np.ndarray            # [S] oturumun günü
    s_chat: np.ndarray           # [S] sohbet indeksi
    s_people: np.ndarray         # düz kişi indeksleri
    s_off: np.ndarray            # [S] embedding_bag offset
    s_feat: np.ndarray           # [S, F]
    target: dict[str, np.ndarray]  # anahtar → [N, V] çok-etiketli hedef
    tmask: dict[str, np.ndarray]   # anahtar → [N] hedef var mı
    rec: dict[str, np.ndarray]     # anahtar → [N, V, N_REC] tazelik (yalnız < t)


def _vocab(raw: Raw, cut: int) -> dict[str, list[str]]:
    sess = {"people": Counter(p for i, _, ps, _ in raw.sessions if i < cut for p in ps),
            "chats": Counter(j for i, j, _, _ in raw.sessions if i < cut)}
    out = {}
    for k in SET_KEYS:
        days = Counter(v for s in raw.sets[k][:cut] if s for v in s)
        out[k] = sorted({v for v, c in days.items() if c >= MIN_DAYS} | {v for v, c in sess[k].items() if c >= MIN_SESS})
    return out


def recency(a: np.ndarray, inc: np.ndarray, quiet: np.ndarray) -> np.ndarray:
    """a[t, v] = v, t gününde aktif mi (hedef tanımı); inc = herhangi bir oturum (gelen dahil);
    quiet = cevapsız oturum (benden mesaj yok) → r[t, v, :] yalnız <t günlerinden."""
    n, v = a.shape
    cs = np.concatenate([np.zeros((1, v), np.float32), np.cumsum(a, 0)])  # cs[t] = sum a[<t]
    t = np.arange(n)

    def window(w):
        return (cs[t + 0] - cs[np.maximum(t - w, 0)]) / w

    def lag1(x):
        return np.concatenate([np.zeros((1, v), np.float32), x[:-1]])

    prev = lag1(a)
    ci = np.concatenate([np.zeros((1, v), np.float32), np.cumsum(inc, 0)])
    same = sum(np.concatenate([np.zeros((7 * k, v), np.float32), a[:-7 * k]])[:n] for k in range(1, 5)) / 4
    since = np.zeros((n, v), np.float32)
    last = np.full(v, -1e9)
    for i in range(n):
        since[i] = np.minimum(i - last, 400)
        last = np.where(a[i] > 0, i, last)
    lag2 = np.concatenate([np.zeros((2, v), np.float32), a[:-2]])[:n]
    lag7 = np.concatenate([np.zeros((7, v), np.float32), a[:-7]])[:n]
    r = np.stack([prev, window(7), window(28), window(180), same, np.log1p(since) / 6,
                  lag1(inc), lag1(quiet), (ci[t] - ci[np.maximum(t - 7, 0)]) / 7, lag2, lag7], -1)
    return r.astype(np.float32)


def tensors(raw: Raw, cut: int) -> Tensors:
    """Sözlük yalnız < cut günlerinden; tazelik tüm günler için ama her t yalnız < t'yi görür."""
    vocab = _vocab(raw, cut)
    ix = {k: {v: j + 1 for j, v in enumerate(vs)} for k, vs in vocab.items()}
    s_day, s_chat, s_people, s_off, s_feat = [], [], [], [], []
    for i, jid, ps, f in raw.sessions:
        s_day.append(i)
        s_chat.append(ix["chats"].get(jid, 0))
        s_off.append(len(s_people))
        s_people.extend(ix["people"].get(p, 0) for p in ps)
        s_feat.append(f)
    n = len(raw.days)
    inc = {k: np.zeros((n, len(vocab[k])), np.float32) for k in SET_KEYS}
    quiet = {k: np.zeros((n, len(vocab[k])), np.float32) for k in SET_KEYS}
    for i, jid, ps, f in raw.sessions:
        items = {"chats": [jid], "people": ps if f[-1] else []}  # f[-1]: DM mi, f[4]: benden mesaj var mı
        for k, vs in items.items():
            for v in vs:
                if v in ix[k]:
                    inc[k][i, ix[k][v] - 1] = 1
                    if not f[4]:
                        quiet[k][i, ix[k][v] - 1] = 1
    target, tmask, rec = {}, {}, {}
    for k in SET_KEYS:
        a = np.zeros((n, len(vocab[k])), np.float32)
        m = np.zeros(n, np.float32)
        for i, s in enumerate(raw.sets[k]):
            if s is None:
                continue
            m[i] = 1
            for v in s:
                if v in ix[k]:
                    a[i, ix[k][v] - 1] = 1
        target[k], tmask[k], rec[k] = a, m, recency(a, inc[k], quiet[k])
    return Tensors(vocab, np.array(s_day), np.array(s_chat), np.array(s_people, dtype=np.int64),
                   np.array(s_off), np.array(s_feat, np.float32).reshape(len(s_day), -1), target, tmask, rec)
