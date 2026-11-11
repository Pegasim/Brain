"""Kişi eşleşmeleri: öneri üretimi + kararlarla birleştirme.

Ingest her kaynaktaki her takma ad (source_kind, alias) için ayrı bir `person` açar; aynı insan
Fotoğraflar'da "Aslı", WhatsApp'ta "Aslı K.", rehberde "Aslı Kaya" olarak üç ayrı kişidir.
Burada:
  1. öneri üretilir: hangi iki takma ad aynı insan olabilir, neden (method) ve ne kadar emin (0..1);
  2. öneriler + Tuna'nın kararları union-find ile kişi gruplarına (kanonik kişi) dönüşür.

Kural: 'different' kararı o iki takma adı ASLA aynı gruba koymaz (dolaylı yoldan da). Eşiği geçen
otomatik öneriler onaysız birleşir ama sitede "otomatik" diye görünür ve reddedilebilir.

cfv.sqlite'ta `person_match` tablosu varsa (ingest üretiyorsa) öneriler oradan okunur; yoksa burada
hesaplanır. Şema önerisi: /srv/cfv/site/PLAN.md §3.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

Key = tuple[str, str]

AUTO_MIN = 0.9  # bu güven ve üstü: onaysız birleşir (Tuna reddedebilir)

# Grup adı seçilirken hangi kaynağın adı önce gelir
NAME_RANK = {"contacts": 0, "iphone-photos": 1, "email": 2, "whatsapp": 3}

METHOD_LABEL = {
    "contacts_phone": "rehber telefonu",
    "name_exact": "aynı isim",
    "name_prefix": "isim kısaltması",
    "first_name": "aynı ilk isim",
    "email_name": "e-posta adı",
}


def norm_name(s: str | None) -> str:
    """Karşılaştırma için ad: Türkçe katlama, aksansız, küçük harf, yalnız harf ve boşluk."""
    if not s:
        return ""
    s = s.replace("İ", "i").replace("I", "ı").lower().replace("ı", "i")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z\s.]", " ", s)
    return " ".join(s.split())


def phone_digits(alias: str) -> str | None:
    """WhatsApp JID ('900001234567@s.whatsapp.net') ya da rehber numarası → yalnız rakam."""
    head = alias.split("@", 1)[0]
    if alias.endswith("@g.us") or "-" in head:
        return None
    d = re.sub(r"\D", "", head)
    return d if len(d) >= 8 else None


def email_local_name(alias: str) -> str:
    """'asli.kaya@ornek.com' → 'asli kaya'."""
    local = alias.split("@", 1)[0]
    return norm_name(re.sub(r"[._\-+0-9]+", " ", local))


@dataclass
class Alias:
    key: Key
    person_id: int
    name: str
    norm: str


@dataclass
class Candidate:
    a: Key
    b: Key
    method: str
    confidence: float
    evidence: dict = field(default_factory=dict)


def load_aliases(conn) -> dict[Key, Alias]:
    out = {}
    for pid, sk, alias, name, disp in conn.execute(
            "SELECT pa.person_id, pa.source_kind, pa.alias, pa.name, p.display_name "
            "FROM person_alias pa JOIN person p ON p.id = pa.person_id WHERE p.is_me = 0"):
        nm = name or disp or alias
        out[(sk, alias)] = Alias((sk, alias), pid, nm, norm_name(nm))
    return out


def _pair(a: Key, b: Key) -> tuple[Key, Key]:
    return (a, b) if a <= b else (b, a)


def compute_candidates(aliases: dict[Key, Alias]) -> list[Candidate]:
    """Farklı kaynaklardaki takma adlar arasında öneriler. Aynı kaynak içinde öneri yok
    (WhatsApp'ta iki 'Deniz' iki ayrı numaradır)."""
    best: dict[tuple[Key, Key], Candidate] = {}

    def put(a: Key, b: Key, method: str, conf: float, **ev):
        if a[0] == b[0]:
            return
        a, b = _pair(a, b)
        cur = best.get((a, b))
        if cur is None or conf > cur.confidence:
            best[(a, b)] = Candidate(a, b, method, conf, ev)

    # 1) telefon: WhatsApp JID ↔ rehber numarası (en güvenilir)
    by_phone: dict[str, list[Key]] = defaultdict(list)
    for k in aliases:
        if k[0] in ("whatsapp", "contacts"):
            d = phone_digits(k[1])
            if d:
                by_phone[d[-10:]].append(k)
    for keys in by_phone.values():
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                put(a, b, "contacts_phone", 0.97)

    # 2) isimler: ilk kelimeye göre kovala, kova içinde karşılaştır
    buckets: dict[str, list[Alias]] = defaultdict(list)
    for al in aliases.values():
        names = [al.norm]
        if al.key[0] == "email":
            names.append(email_local_name(al.key[1]))
        for n in set(filter(None, names)):
            buckets[n.split()[0]].append((al, n))
    for first, items in buckets.items():
        if len(items) > 60:  # "ahmet" gibi çok yaygın tek isimde patlamasın
            items = [it for it in items if len(it[1].split()) > 1]
        firsts = defaultdict(int)
        for al, n in items:
            firsts[al.key[0]] += 1
        for i, (a, na) in enumerate(items):
            for b, nb in items[i + 1:]:
                if a.key[0] == b.key[0]:
                    continue
                ta, tb = na.replace(".", "").split(), nb.replace(".", "").split()
                via_email = na != a.norm or nb != b.norm  # e-posta adresinden çıkarılan ad
                if na.replace(".", "") == nb.replace(".", "") and len(ta) > 1:
                    put(a.key, b.key, "email_name" if via_email else "name_exact",
                        0.7 if via_email else 0.8, names=[a.name, b.name])
                elif len(ta) > 1 and len(tb) > 1 and ta[0] == tb[0] and (
                        ta[1].startswith(tb[1]) or tb[1].startswith(ta[1])):
                    put(a.key, b.key, "name_prefix", 0.6, names=[a.name, b.name])
                elif len(ta) == 1 or len(tb) == 1:
                    # yalnız ilk isim: o kaynakta bu ilk isimle tek kişi varsa daha anlamlı
                    unique = firsts[a.key[0]] == 1 and firsts[b.key[0]] == 1
                    put(a.key, b.key, "first_name", 0.45 if unique else 0.25,
                        names=[a.name, b.name], unique=unique)
    return list(best.values())


def read_candidates(conn, aliases: dict[Key, Alias]) -> list[Candidate]:
    """Ingest person_match tablosunu üretiyorsa onu kullan, yoksa hesapla."""
    has = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='person_match'").fetchone()
    if not has:
        return compute_candidates(aliases)
    out = []
    for r in conn.execute("SELECT a_kind, a_alias, b_kind, b_alias, method, confidence, evidence "
                          "FROM person_match"):
        a, b = _pair((r[0], r[1]), (r[2], r[3]))
        if a in aliases and b in aliases:
            out.append(Candidate(a, b, r[4], r[5], json.loads(r[6]) if r[6] else {}))
    return out


@dataclass
class Edge:
    a: Key
    b: Key
    via: str               # 'auto' | 'tuna'
    method: str            # öneri yöntemi ya da 'manual'
    confidence: float
    decision_id: int | None = None


class Groups:
    """Union-find; 'different' kısıtlarını grup düzeyinde korur."""

    def __init__(self, keys):
        self.parent = {k: k for k in keys}
        self.members = {k: {k} for k in keys}
        self.blocked: dict[Key, set[Key]] = defaultdict(set)  # takma ad → asla birleşmeyeceği adlar

    def find(self, k: Key) -> Key:
        while self.parent[k] != k:
            self.parent[k] = self.parent[self.parent[k]]
            k = self.parent[k]
        return k

    def block(self, a: Key, b: Key):
        self.blocked[a].add(b)
        self.blocked[b].add(a)

    def conflict(self, ra: Key, rb: Key) -> bool:
        small, big = sorted((self.members[ra], self.members[rb]), key=len)
        return any(self.blocked[m] & big for m in small)

    def union(self, a: Key, b: Key) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        if self.conflict(ra, rb):
            return False
        if len(self.members[ra]) < len(self.members[rb]):
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.members[ra] |= self.members.pop(rb)
        return True


def build(aliases: dict[Key, Alias], candidates: list[Candidate], effective: dict) -> dict:
    """→ {'group_of': Key→kök, 'groups': kök→[Key], 'edges': kök→[Edge], 'status': çift→durum}.

    durum: 'auto' (otomatik birleşti), 'same'/'different' (Tuna), 'pending', 'blocked'
    (otomatik ama 'different' kısıtı engelledi), 'implied' (başka bağlarla zaten aynı grupta)."""
    g = Groups(aliases.keys())
    status: dict[tuple[Key, Key], str] = {}
    edges: list[Edge] = []
    for (a, b), d in effective.items():
        if d["verdict"] == "different" and a in aliases and b in aliases:
            g.block(a, b)
    # önce Tuna'nın 'same' kararları (eski → yeni), sonra otomatikler (güvenli → az güvenli)
    for (a, b), d in sorted(effective.items(), key=lambda kv: kv[1]["id"]):
        if d["verdict"] == "same" and a in aliases and b in aliases:
            if g.union(a, b):
                edges.append(Edge(a, b, "tuna", "manual", 1.0, d["id"]))
    cands = sorted(candidates, key=lambda c: -c.confidence)
    for c in cands:
        d = effective.get((c.a, c.b))
        if d:
            status[(c.a, c.b)] = d["verdict"]
            if d["verdict"] == "same":
                for e in edges:
                    if (e.a, e.b) == (c.a, c.b):
                        e.method, e.confidence = c.method, c.confidence
            continue
        if c.confidence >= AUTO_MIN:
            if g.find(c.a) == g.find(c.b):
                status[(c.a, c.b)] = "implied"
            elif g.union(c.a, c.b):
                status[(c.a, c.b)] = "auto"
                edges.append(Edge(c.a, c.b, "auto", c.method, c.confidence))
            else:
                status[(c.a, c.b)] = "blocked"
    for c in cands:
        if (c.a, c.b) not in status:
            status[(c.a, c.b)] = "implied" if g.find(c.a) == g.find(c.b) else "pending"
    group_of = {k: g.find(k) for k in aliases}
    groups: dict[Key, list[Key]] = defaultdict(list)
    for k, r in group_of.items():
        groups[r].append(k)
    gedges: dict[Key, list[Edge]] = defaultdict(list)
    for e in edges:
        gedges[group_of[e.a]].append(e)
    return {"group_of": group_of, "groups": dict(groups), "edges": dict(gedges), "status": status}


def group_name(keys: list[Key], aliases: dict[Key, Alias]) -> str:
    def rank(k):
        al = aliases[k]
        return (NAME_RANK.get(k[0], 9), -len(al.norm.split()), al.name)
    return aliases[min(keys, key=rank)].name
