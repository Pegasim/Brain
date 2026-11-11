"""Site veri katmanı: cfv.sqlite SALT-OKUR + kararlar → zaman çizgisi / ağ / kişi yanıtları.

Model (kişi grupları, günlük sayımlar) DB değişene ya da karar yazılana kadar bellekte tutulur.
Pencere sorguları bu günlük sayımlardan hesaplanır; WhatsApp yüz binlerce mesaj olsa da her
kaydırmada tabloyu taramayız.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import sqlite3
import threading
from bisect import bisect_left, bisect_right
from collections import defaultdict
from pathlib import Path

from cfv import db
from cfv_site import match
from cfv_site.decisions import Decisions, ordered

PHOTO_KINDS = ("photo", "video")
DENSITY_KINDS = {"photo": "photo", "video": "photo", "whatsapp": "whatsapp", "calendar": "calendar",
                 "note": "note", "email": "email"}
# kişinin o dönemdeki ağırlığı: bir ortak etkinlik ≈ 15 mesaj
WEIGHT = {"whatsapp": 1, "email": 2, "photo": 4, "video": 4, "note": 3, "calendar": 15}
MAX = {"person": 45, "calendar": 45, "photos": 45, "note": 20, "chat": 12}


def open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _db_sig(path: Path) -> tuple:
    out = []
    for p in (path, path.with_name(path.name + "-wal")):
        try:
            st = p.stat()
            out.append((st.st_mtime_ns, st.st_size))
        except FileNotFoundError:
            out.append(None)
    return tuple(out)


def _day_add(d: str, n: int) -> str:
    return (dt.date.fromisoformat(d) + dt.timedelta(days=n)).isoformat()


def bucket_of(d: str, step: str) -> str:
    if step == "month":
        return d[:7] + "-01"
    if step == "week":
        x = dt.date.fromisoformat(d)
        return (x - dt.timedelta(days=x.weekday())).isoformat()
    return d


def _bucket_end(b: str, step: str) -> str:
    if step == "month":
        x = dt.date.fromisoformat(b)
        nxt = dt.date(x.year + (x.month == 12), x.month % 12 + 1, 1)
        return (nxt - dt.timedelta(days=1)).isoformat()
    return _day_add(b, 6 if step == "week" else 0)


def step_for(span_days: int) -> str:
    return "month" if span_days > 900 else "week" if span_days > 120 else "day"


class Model:
    """Bir DB anlık görüntüsü + karar sürümü için hesaplanmış her şey."""

    def __init__(self, conn: sqlite3.Connection, decisions: Decisions):
        self.aliases = match.load_aliases(conn)
        self.candidates = match.read_candidates(conn, self.aliases)
        self.effective = decisions.effective()
        r = match.build(self.aliases, self.candidates, self.effective)
        self.group_of, self.edges, self.status = r["group_of"], r["edges"], r["status"]
        # kanonik kişi kimliği = grubun en küçük person.id'si (ekranda geçici; kararlar alias'a bağlı)
        self.canon_of_person: dict[int, int] = {}
        self.members: dict[int, list[match.Key]] = {}
        self.name: dict[int, str] = {}
        root_canon: dict[match.Key, int] = {}
        for root, keys in r["groups"].items():
            cid = min(self.aliases[k].person_id for k in keys)
            root_canon[root] = cid
            self.members[cid] = sorted(keys)
            self.name[cid] = match.group_name(keys, self.aliases)
            for k in keys:
                self.canon_of_person[self.aliases[k].person_id] = cid
        self.canon_of_key = {k: root_canon[root] for k, root in self.group_of.items()}
        self.root_of_canon = {c: root for root, c in root_canon.items()}

        # kişi × gün × tür sayımı (tek tarama)
        self.pday: dict[int, list[tuple[str, str, int]]] = defaultdict(list)
        for pid, day, kind, n in conn.execute(
                "SELECT ep.person_id, e.local_date, e.kind, count(DISTINCT e.id) FROM event_person ep "
                "JOIN event e ON e.id = ep.event_id WHERE e.kind != 'dream' GROUP BY 1, 2, 3 ORDER BY 2"):
            cid = self.canon_of_person.get(pid)
            if cid is not None:
                self.pday[cid].append((day, kind, n))
        self.pday_days = {c: [r[0] for r in rows] for c, rows in self.pday.items()}

        # gün × tür yoğunluğu
        self.days: list[str] = []
        self.dens: list[dict[str, int]] = []
        cur = None
        for day, kind, n in conn.execute(
                "SELECT local_date, kind, count(*) FROM event GROUP BY 1, 2 ORDER BY 1"):
            k = DENSITY_KINDS.get(kind)
            if not k:
                continue
            if day != cur:
                self.days.append(day)
                self.dens.append(defaultdict(int))
                cur = day
            self.dens[-1][k] += n

        # grup sohbetleri: sohbet × gün mesaj sayısı + üyeler
        self.chat_day: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for chat, day, n in conn.execute(
                "SELECT json_extract(payload, '$.chat_jid'), local_date, count(*) FROM event "
                "WHERE kind = 'whatsapp' AND json_extract(payload, '$.chat_type') = 'group' "
                "GROUP BY 1, 2 ORDER BY 2"):
            self.chat_day[chat].append((day, n))
        self.chat_name = {jid: name for jid, name in conn.execute(
            "SELECT json_extract(payload, '$.chat_jid'), max(title) FROM event "
            "WHERE kind = 'whatsapp' AND json_extract(payload, '$.chat_type') = 'group' GROUP BY 1")}
        self.chat_members: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        for chat, pid, n in conn.execute(
                "SELECT json_extract(e.payload, '$.chat_jid'), ep.person_id, count(*) FROM event e "
                "JOIN event_person ep ON ep.event_id = e.id WHERE e.kind = 'whatsapp' "
                "AND ep.role = 'sender' GROUP BY 1, 2"):
            cid = self.canon_of_person.get(pid)
            if cid is not None:
                self.chat_members[chat][cid] += n
        self.synthetic = bool(conn.execute(
            "SELECT 1 FROM meta WHERE key = 'synthetic' AND value = '1'").fetchone())

    # -- yardımcılar
    def person_ids(self, cid: int) -> list[int]:
        return sorted({self.aliases[k].person_id for k in self.members.get(cid, [])})

    def person_rows(self, cid: int, d0: str, d1: str):
        days = self.pday_days.get(cid, [])
        rows = self.pday.get(cid, [])
        return rows[bisect_left(days, d0):bisect_right(days, d1)]


class NotSynthetic(RuntimeError):
    """CFV_SITE_REQUIRE_SYNTHETIC=1 iken DB'de meta.synthetic=1 yok: gerçek veri sunulmaz."""


class Store:
    def __init__(self, db_path: Path | None = None, decisions: Decisions | None = None,
                 require_synthetic: bool | None = None):
        if require_synthetic is None:
            require_synthetic = os.environ.get("CFV_SITE_REQUIRE_SYNTHETIC") == "1"
        self.require_synthetic = require_synthetic
        self.db_path = db_path or db.default_db_path()
        self.decisions = decisions or Decisions()
        self._lock = threading.Lock()
        self._model: Model | None = None
        self._key = None

    def conn(self) -> sqlite3.Connection:
        return open_ro(self.db_path)

    def model(self) -> Model:
        key = (_db_sig(self.db_path), self.decisions.version)
        with self._lock:
            if self._model is None or key != self._key:
                with self.conn() as c:
                    if self.require_synthetic and not c.execute(
                            "SELECT 1 FROM meta WHERE key = 'synthetic' AND value = '1'").fetchone():
                        self._model = None
                        raise NotSynthetic(str(self.db_path))
                    self._model = Model(c, self.decisions)
                self._key = key
            return self._model

    # ---------------------------------------------------------------- uçlar
    def meta(self) -> dict:
        m = self.model()
        pending = sum(1 for x in self.grouped_candidates(m) if x["status"] == "pending")
        return {"first": m.days[0] if m.days else None, "last": m.days[-1] if m.days else None,
                "synthetic": m.synthetic, "people": len(m.members), "pending_matches": pending}

    def density(self, d0: str, d1: str, step: str | None = None) -> dict:
        m = self.model()
        span = (dt.date.fromisoformat(d1) - dt.date.fromisoformat(d0)).days
        step = step or ("day" if span <= 400 else "week")
        out: dict[str, dict[str, int]] = {}
        for i in range(bisect_left(m.days, d0), bisect_right(m.days, d1)):
            b = out.setdefault(bucket_of(m.days[i], step), defaultdict(int))
            for k, n in m.dens[i].items():
                b[k] += n
        return {"step": step, "buckets": [{"d": d, **v} for d, v in sorted(out.items())]}

    def window(self, d0: str, d1: str) -> dict:
        """Görünen aralığın düğümleri ve bağları. Çok uzun aralıkta toplanmış (aylık/haftalık)."""
        m = self.model()
        span = max(1, (dt.date.fromisoformat(d1) - dt.date.fromisoformat(d0)).days)
        step = step_for(span)
        nodes, links = [], []

        # -- kişiler
        scored = []
        for cid in m.pday:
            rows = m.person_rows(cid, d0, d1)
            if not rows:
                continue
            w = tw = 0.0
            counts = defaultdict(int)
            for day, kind, n in rows:
                x = WEIGHT.get(kind, 1) * n
                w += x
                tw += x * dt.date.fromisoformat(day).toordinal()
                counts[kind] += n
            scored.append((w, cid, tw / w, counts, rows[0][0], rows[-1][0]))
        scored.sort(reverse=True)
        shown = {}
        for w, cid, tmean, counts, first, last in scored[:MAX["person"]]:
            shown[cid] = True
            nodes.append({"id": f"p:{cid}", "type": "person", "label": m.name[cid], "w": round(w, 1),
                          "t": dt.date.fromordinal(round(tmean)).isoformat(), "t0": first, "t1": last,
                          "counts": dict(counts)})

        with self.conn() as c:
            # -- takvim olayları (önem: süre + foto + katılımcı)
            cal = c.execute("""
                SELECT e.id, e.title, e.start_utc, e.end_utc, e.local_date, e.time_quality,
                       (SELECT count(*) FROM event_link l WHERE l.b = e.id AND l.kind = 'photo_at') photos,
                       (SELECT group_concat(person_id) FROM event_person WHERE event_id = e.id) people
                FROM event e WHERE e.kind = 'calendar' AND e.local_date BETWEEN ? AND ?""", (d0, d1)).fetchall()
            def cal_score(r):
                days = 0
                if r["end_utc"]:
                    days = (dt.datetime.fromisoformat(r["end_utc"].replace("Z", "+00:00")) -
                            dt.datetime.fromisoformat(r["start_utc"].replace("Z", "+00:00"))).total_seconds() / 86400
                ppl = len(r["people"].split(",")) if r["people"] else 0
                return (1 + r["photos"] / 4 + ppl * 2 + (8 * days if days >= 1 else 0))
            cal = sorted(cal, key=cal_score, reverse=True)[:MAX["calendar"]]
            cal_ids = []
            for r in cal:
                cal_ids.append(r["id"])
                nodes.append({"id": f"c:{r['id']}", "type": "calendar", "label": r["title"] or "(adsız)",
                              "t": r["local_date"], "w": round(cal_score(r), 1), "photos": r["photos"],
                              "start": r["start_utc"], "end": r["end_utc"], "allday": r["time_quality"] == "day"})
                for pid in {m.canon_of_person.get(int(p)) for p in (r["people"] or "").split(",") if p}:
                    if pid in shown:
                        links.append({"s": f"p:{pid}", "t": f"c:{r['id']}", "k": "attendee"})

            # -- foto kümeleri: kova (gün/hafta/ay) başına bir düğüm
            pb: dict[str, dict] = {}
            for r in c.execute(
                    "SELECT e.local_date d, count(*) n, min(m.id) mid FROM event e JOIN media m ON m.event_id = e.id "
                    "WHERE e.kind IN ('photo','video') AND e.local_date BETWEEN ? AND ? GROUP BY 1", (d0, d1)):
                b = bucket_of(r["d"], step)
                x = pb.setdefault(b, {"n": 0, "mid": r["mid"], "days": []})
                x["n"] += r["n"]
                x["days"].append(r["d"])
            top = sorted(pb.items(), key=lambda kv: -kv[1]["n"])[:MAX["photos"]]
            for b, x in top:
                end = _bucket_end(b, step)
                nid = f"f:{b}:{step}"
                nodes.append({"id": nid, "type": "photos", "label": f"{x['n']} foto", "t": b, "w": x["n"],
                              "n": x["n"], "media": x["mid"], "from": b, "to": end,
                              "tmid": x["days"][len(x["days"]) // 2]})
                # kişi ↔ foto kümesi: o günlerde fotoda görünen gösterilen kişiler
                dset = set(x["days"])
                for cid in shown:
                    n = sum(k for day, kind, k in m.person_rows(cid, min(dset), max(dset))
                            if kind in PHOTO_KINDS and day in dset)
                    if n:
                        links.append({"s": f"p:{cid}", "t": nid, "k": "in_photo", "n": n})
                # foto kümesi ↔ takvim (photo_at)
            if cal_ids and top:
                bucket_node = {b: f"f:{b}:{step}" for b, _ in top}
                q = ",".join("?" * len(cal_ids))
                for b_id, d in c.execute(
                        f"SELECT DISTINCT l.b, p.local_date FROM event_link l JOIN event p ON p.id = l.a "
                        f"WHERE l.kind = 'photo_at' AND l.b IN ({q})", cal_ids):
                    nid = bucket_node.get(bucket_of(d, step))
                    if nid:
                        links.append({"s": f"c:{b_id}", "t": nid, "k": "photo_at"})

            # -- notlar
            for r in c.execute(
                    "SELECT e.id, e.title, e.local_date, length(e.body) L, "
                    "(SELECT group_concat(person_id) FROM event_person WHERE event_id = e.id) people "
                    "FROM event e WHERE e.kind = 'note' AND e.local_date BETWEEN ? AND ? "
                    "ORDER BY L DESC LIMIT ?", (d0, d1, MAX["note"])):
                nodes.append({"id": f"n:{r['id']}", "type": "note", "label": r["title"] or "(not)",
                              "t": r["local_date"], "w": r["L"] or 1})
                for pid in {m.canon_of_person.get(int(p)) for p in (r["people"] or "").split(",") if p}:
                    if pid in shown:
                        links.append({"s": f"p:{pid}", "t": f"n:{r['id']}", "k": "mentioned"})

        # -- grup sohbetleri
        chats = []
        for jid, rows in m.chat_day.items():
            days = [d for d, _ in rows]
            sel = rows[bisect_left(days, d0):bisect_right(days, d1)]
            n = sum(k for _, k in sel)
            if n:
                tmean = sum(dt.date.fromisoformat(d).toordinal() * k for d, k in sel) / n
                chats.append((n, jid, tmean))
        for n, jid, tmean in sorted(chats, reverse=True)[:MAX["chat"]]:
            nid = "g:" + hashlib.sha1(jid.encode()).hexdigest()[:12]
            nodes.append({"id": nid, "type": "chat", "label": m.chat_name.get(jid) or "grup", "w": n,
                          "t": dt.date.fromordinal(round(tmean)).isoformat(), "n": n})
            for cid, k in m.chat_members[jid].items():
                if cid in shown:
                    links.append({"s": f"p:{cid}", "t": nid, "k": "member", "n": k})

        return {"from": d0, "to": d1, "step": step, "nodes": nodes, "links": links}

    # ---------------------------------------------------------------- kişi
    def alias_view(self, m: Model, k: match.Key) -> dict:
        al = m.aliases[k]
        shown = k[1]
        if k[0] in ("whatsapp", "contacts"):
            d = match.phone_digits(k[1])
            shown = f"…{d[-4:]}" if d else k[1]
        elif k[0] == "iphone-photos":
            shown = "yüz etiketi"
        return {"kind": k[0], "alias": k[1], "name": al.name, "shown": shown}

    def person(self, cid: int) -> dict | None:
        m = self.model()
        if cid not in m.members:
            return None
        weeks: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        totals = defaultdict(int)
        for day, kind, n in m.pday.get(cid, []):
            weeks[bucket_of(day, "week")][DENSITY_KINDS.get(kind, kind)] += n
            totals[kind] += n
        pids = m.person_ids(cid)
        q = ",".join("?" * len(pids))
        with self.conn() as c:
            photos = [dict(r) for r in c.execute(
                f"SELECT DISTINCT m.id, e.local_date d, e.kind FROM event_person ep JOIN event e ON e.id = ep.event_id "
                f"JOIN media m ON m.event_id = e.id WHERE ep.person_id IN ({q}) AND e.kind IN ('photo','video') "
                f"AND m.role = 'original' ORDER BY e.start_utc DESC LIMIT 240", pids)]
            events = [dict(r) for r in c.execute(
                f"SELECT DISTINCT e.id, e.title, e.local_date d, e.kind, ep.role FROM event_person ep "
                f"JOIN event e ON e.id = ep.event_id WHERE ep.person_id IN ({q}) "
                f"AND e.kind IN ('calendar','note') ORDER BY e.start_utc DESC LIMIT 200", pids)]
        root = m.root_of_canon[cid]
        edges = [{"a": self.alias_view(m, e.a), "b": self.alias_view(m, e.b), "via": e.via,
                  "method": e.method, "method_label": match.METHOD_LABEL.get(e.method, "elle"),
                  "confidence": e.confidence, "decision_id": e.decision_id}
                 for e in m.edges.get(root, [])]
        mine = set(m.members[cid])
        cands = [self.candidate_view(m, x) for x in self.grouped_candidates(m)
                 if x["status"] == "pending" and (x["cand"].a in mine or x["cand"].b in mine)]
        groups = sorted(((n, m.chat_name.get(j, "grup")) for j, mem in m.chat_members.items()
                         for c2, n in mem.items() if c2 == cid), reverse=True)
        return {"id": cid, "name": m.name[cid], "aliases": [self.alias_view(m, k) for k in m.members[cid]],
                "totals": dict(totals), "weeks": [{"d": d, **v} for d, v in sorted(weeks.items())],
                "photos": photos, "events": events, "edges": edges, "candidates": cands,
                "groups": [{"name": g, "n": n} for n, g in groups[:10]]}

    def search_people(self, q: str, limit: int = 12) -> list[dict]:
        m = self.model()
        nq = match.norm_name(q)
        if not nq:
            return []
        hits = []
        for cid, keys in m.members.items():
            names = {m.aliases[k].norm for k in keys}
            if any(nq in n for n in names):
                total = sum(n for _, _, n in m.pday.get(cid, []))
                hits.append((not any(n.startswith(nq) for n in names), -total, cid))
        hits.sort()
        return [{"id": cid, "name": m.name[cid], "total": -t} for _, t, cid in hits[:limit]]

    def photos(self, d0: str, d1: str, limit: int = 300) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT m.id, e.local_date d, e.kind FROM event e JOIN media m ON m.event_id = e.id "
                "WHERE e.kind IN ('photo','video') AND m.role = 'original' AND e.local_date BETWEEN ? AND ? "
                "ORDER BY e.start_utc LIMIT ?", (d0, d1, limit))]

    def calendar_event(self, eid: int) -> dict | None:
        m = self.model()
        with self.conn() as c:
            r = c.execute("SELECT id, title, body, start_utc, end_utc, local_date, time_quality FROM event "
                          "WHERE id = ? AND kind IN ('calendar','note')", (eid,)).fetchone()
            if not r:
                return None
            ppl = {m.canon_of_person.get(p) for p, in c.execute(
                "SELECT person_id FROM event_person WHERE event_id = ?", (eid,))}
            photos = [dict(x) for x in c.execute(
                "SELECT m.id, p.local_date d, p.kind FROM event_link l JOIN event p ON p.id = l.a "
                "JOIN media m ON m.event_id = p.id WHERE l.b = ? AND l.kind = 'photo_at' AND m.role = 'original' "
                "ORDER BY p.start_utc LIMIT 120", (eid,))]
        return {**dict(r), "people": [{"id": p, "name": m.name[p]} for p in ppl if p in m.name],
                "photos": photos}

    def media_path(self, mid: int) -> tuple[str, str, str, str | None] | None:
        """İstemci yalnız media.id bilir; yol her zaman DB'den gelir (yol dolaşma yok).
        → (dosya yolu, mime, yerel gün, foto ise asset UUID)"""
        with self.conn() as c:
            r = c.execute("SELECT m.path, m.mime, e.local_date, e.kind, e.external_id FROM media m "
                          "JOIN event e ON e.id = m.event_id WHERE m.id = ?", (mid,)).fetchone()
        if not r:
            return None
        uuid = r["external_id"] if r["kind"] in PHOTO_KINDS else None
        return r["path"], r["mime"] or "", r["local_date"], uuid

    # ---------------------------------------------------------------- eşleşmeler
    def grouped_candidates(self, m: Model) -> list[dict]:
        """Aynı iki kişi grubu arasındaki öneriler tek kart: en güvenlisi + diğer yöntemler."""
        by: dict[tuple, dict] = {}
        for c in sorted(m.candidates, key=lambda c: -c.confidence):
            st = m.status[(c.a, c.b)]
            ga, gb = m.canon_of_key[c.a], m.canon_of_key[c.b]
            gk = (min(ga, gb), max(ga, gb), st if st != "pending" else "pending") if st in ("pending", "implied") \
                else (c.a, c.b)
            if gk in by:
                by[gk]["also"].append(match.METHOD_LABEL.get(c.method, c.method))
                continue
            by[gk] = {"cand": c, "status": st, "also": []}
        return [v for v in by.values() if v["status"] != "implied"]

    def candidate_view(self, m: Model, x: dict) -> dict:
        c = x["cand"]
        dec = m.effective.get(ordered(c.a, c.b))
        def side(k):
            cid = m.canon_of_key[k]
            rows = m.pday.get(cid, [])
            tot = defaultdict(int)
            for _, kind, n in rows:
                tot[kind] += n
            return {**self.alias_view(m, k), "person": cid, "group": m.name[cid],
                    "group_size": len(m.members[cid]), "totals": dict(tot),
                    "first": rows[0][0] if rows else None, "last": rows[-1][0] if rows else None}
        return {"a": side(c.a), "b": side(c.b), "method": c.method,
                "method_label": match.METHOD_LABEL.get(c.method, c.method), "also": x["also"],
                "confidence": c.confidence, "evidence": c.evidence, "status": x["status"],
                "decision_id": dec["id"] if dec else None}

    def matches(self, status: str = "pending") -> dict:
        m = self.model()
        items = [self.candidate_view(m, x) for x in self.grouped_candidates(m)
                 if status == "all" or x["status"] == status]
        hist = []
        for d in reversed(self.decisions.all()):
            a, b = (d["a_kind"], d["a_alias"]), (d["b_kind"], d["b_alias"])
            if a in m.aliases and b in m.aliases:
                hist.append({"id": d["id"], "at": d["at"], "verdict": d["verdict"], "undone_at": d["undone_at"],
                             "a": self.alias_view(m, a), "b": self.alias_view(m, b)})
        counts = defaultdict(int)
        for x in self.grouped_candidates(m):
            counts[x["status"]] += 1
        return {"items": items, "history": hist[:200], "counts": dict(counts)}

    def decide(self, a: match.Key, b: match.Key, verdict: str, note: str | None = None) -> int:
        m = self.model()
        if a not in m.aliases or b not in m.aliases:
            raise KeyError("takma ad yok")
        return self.decisions.add(a, b, verdict, note)

    def link_people(self, c1: int, c2: int) -> int:
        """Elle bağlama: iki kişi grubunu 'aynı' yap (her grubun ilk takma adı üzerinden)."""
        m = self.model()
        if c1 not in m.members or c2 not in m.members or c1 == c2:
            raise KeyError("kişi")
        return self.decisions.add(m.members[c1][0], m.members[c2][0], "same", "elle bağlandı")
