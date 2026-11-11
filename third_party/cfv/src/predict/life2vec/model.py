"""Olay embedding'i → gün havuzu → GRU → çok başlı çıktı."""

from __future__ import annotations

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from .data import CONT, N_CREC, N_REC, SET_KEYS, Raw, Tensors


class Life2Vec(nn.Module):
    def __init__(self, raw: Raw, tn: Tensors, d: int = 32, h: int = 64, drop: float = 0.3):
        super().__init__()
        self.emb = nn.ModuleDict({k: nn.Embedding(len(tn.vocab[k]) + 1, d) for k in SET_KEYS})
        self.sfeat = nn.Linear(tn.s_feat.shape[1], d)
        n_ctx = raw.ctx.shape[1]
        self.inp = nn.Sequential(nn.Linear(raw.x.shape[1] + d + n_ctx, h), nn.GELU(), nn.Dropout(drop))
        self.gru = nn.GRU(h, h, batch_first=True)
        self.head = nn.Sequential(nn.Linear(h + n_ctx, h), nn.GELU(), nn.Dropout(drop))
        self.mu = nn.Linear(h, raw.y.shape[1])
        self.log_r = nn.Linear(h, raw.y.shape[1])  # güne bağlı dispersiyon
        nn.init.zeros_(self.log_r.weight)
        nn.init.zeros_(self.log_r.bias)
        w = torch.zeros(raw.y.shape[1], N_CREC)
        w[:, 2] = 1.0  # başlangıç: log μ ≈ log(1 + 28 gün ortalaması)
        self.cw = nn.Parameter(w)
        self.q = nn.ModuleDict({k: nn.Linear(h, d) for k in SET_KEYS})
        self.bias = nn.ParameterDict({k: nn.Parameter(torch.zeros(len(tn.vocab[k]) + 1)) for k in SET_KEYS})
        self.rec = nn.ModuleDict({k: nn.Sequential(nn.Linear(N_REC, 16), nn.GELU(), nn.Linear(16, 1))
                                  for k in SET_KEYS})
        # öğeye özgü tazelik ağırlığı (embedding'den): "bu kişi dün yazdıysa bugün de yazarım" öğeden öğeye değişir
        self.rw = nn.ModuleDict({k: nn.Linear(d, N_REC) for k in SET_KEYS})
        for k in SET_KEYS:
            nn.init.zeros_(self.rw[k].weight)
            nn.init.zeros_(self.rw[k].bias)

    def forward(self, b: dict) -> tuple[torch.Tensor, torch.Tensor, dict]:
        """b: tensör sözlüğü (`batch`). Çıktı t indeksli: t'yi yalnız x[<t] ve ctx[t] belirler."""
        n = b["x"].shape[0]
        ev = (self.emb["chats"](b["s_chat"])
              + F.embedding_bag(b["s_people"], self.emb["people"].weight, b["s_off"], mode="mean")
              + self.sfeat(b["s_feat"]))
        pool = torch.zeros(n, ev.shape[1], device=ev.device).index_add_(0, b["s_day"], ev)
        pool = pool / b["s_count"].clamp(min=1).unsqueeze(1)
        hs, _ = self.gru(self.inp(torch.cat([b["x"], pool, b["ctx"]], 1)).unsqueeze(0))
        prev = torch.cat([torch.zeros_like(hs[0, :1]), hs[0, :-1]])  # h[t-1]
        z = self.head(torch.cat([prev, b["ctx"]], 1))
        logits = {k: self.q[k](z) @ self.emb[k].weight[1:].T + self.bias[k][1:]
                  + self.rec[k](b["rec"][k]).squeeze(-1)
                  + (self.rw[k](self.emb[k].weight[1:]) * b["rec"][k]).sum(-1) for k in SET_KEYS}
        return self.mu(z) + (b["crec"] * self.cw).sum(-1), self.log_r(z), logits


def batch(raw: Raw, tn: Tensors, upto: int, device) -> dict:
    """Günler [0, upto) — tahmin yalnız bu aralıktaki t'ler için yapılır."""
    k = int(np.searchsorted(tn.s_day, upto))  # oturumlar güne göre sıralı → önek
    np_ = int(tn.s_off[k]) if k < len(tn.s_off) else len(tn.s_people)
    s_day = torch.as_tensor(tn.s_day[:k])
    t = lambda a, dt=torch.float32: torch.as_tensor(a, dtype=dt, device=device)
    return {
        "x": t(raw.x[:upto]), "ctx": t(raw.ctx[:upto]), "y": t(raw.y[:upto]), "crec": t(raw.crec[:upto]),
        "s_day": s_day.to(device), "s_chat": t(tn.s_chat[:k], torch.long),
        "s_people": t(tn.s_people[:np_], torch.long), "s_off": t(tn.s_off[:k], torch.long),
        "s_feat": t(tn.s_feat[:k]),
        "s_count": torch.bincount(s_day, minlength=upto).float().to(device),
        "rec": {k: t(tn.rec[k][:upto]) for k in SET_KEYS},
        "target": {k: t(tn.target[k][:upto]) for k in SET_KEYS},
        "tmask": {k: t(tn.tmask[k][:upto]) for k in SET_KEYS},
    }


def nb_nll(log_mu: torch.Tensor, log_r: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    mu, r = log_mu.exp(), log_r.exp()
    return -(torch.lgamma(y + r) - torch.lgamma(r) - torch.lgamma(y + 1)
             + r * (log_r - torch.logaddexp(log_r, log_mu)) + y * (log_mu - torch.logaddexp(log_r, log_mu)))


def loss(model: Life2Vec, out, b: dict, days: torch.Tensor) -> torch.Tensor:
    """days: kayba giren t'lerin bool maskesi. Hedef başına ortalama NLL + küme başına gün ortalaması BCE.
    Sayılar negatif binom (μ = exp(log_mu), r = exp(log_r)); CONT hedefleri log1p(y) ~ Laplace(log_mu, exp(log_r))."""
    log_mu, log_r, logits = out
    y = b["y"][days]
    m = ~torch.isnan(y)
    y0 = torch.nan_to_num(y)
    cont = torch.as_tensor(CONT, device=y.device)  # sürekli: log1p ölçeğinde Laplace (konum = medyan)
    lap = (torch.log1p(y0) - log_mu[days]).abs() / log_r[days].exp() + log_r[days]
    nll = torch.where(cont, lap, nb_nll(log_mu[days], log_r[days], y0)) * m
    total = (nll.sum(0) / m.sum(0).clamp(min=1)).sum()
    for k in SET_KEYS:
        tm = b["tmask"][k][days]
        bce = F.binary_cross_entropy_with_logits(logits[k][days], b["target"][k][days], reduction="none").sum(1)
        total = total + (bce * tm).sum() / tm.sum().clamp(min=1)
    return total
