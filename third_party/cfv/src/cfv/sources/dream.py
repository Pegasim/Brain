"""Rüyalar (cfv.ruya toplar) → 'dream' olayları + `dream` / `dream_eval` tabloları + kişi önerileri.

event.body BOŞ: metin yalnız `dream` tablosunda (FTS, ağaç, site, raporlar görmez).
Değerlendirme data/processed/ruya/is/<iş>.json'dan her ingest'te yeniden yüklenir; istemde olmayan
kişi/olay ref'leri atılır (uydurma id). Kişi önerisi event_person(role='dream'): Tuna 'hayir' dediyse
yazılmaz, 'evet' dediyse güven 1.0, yoksa en çok 0.6. Bu rol birlikte-bulunma sayımına girmez.
"""

from __future__ import annotations

import json

from cfv import ruya
from cfv.ingest import Batch

IMPORTER = "cfv.sources.dream@1"


def _f(x, lo, hi, cast=float):
    try:
        return max(lo, min(hi, cast(x)))
    except (TypeError, ValueError):
        return None


def load_eval(key: str) -> tuple[dict, dict] | None:
    out, meta = ruya.jobs_dir() / f"{key}.json", ruya.jobs_dir() / f"{key}.meta.json"
    if not out.exists() or not meta.exists():
        return None
    try:
        e = json.loads(out.read_text().strip().removeprefix("```json").removesuffix("```"))
    except json.JSONDecodeError:
        return None
    m = json.loads(meta.read_text())
    P, E = set(m["refs"]["P"]), set(m["refs"]["E"])
    e["kisiler"] = [{**k, "person": k.get("person") if k.get("person") in P else None,
                     "guven": _f(k.get("guven"), 0, 1)} for k in e.get("kisiler") or [] if isinstance(k, dict)]
    e["gunduz_baglantisi"] = [{**g, "guc": _f(g.get("guc"), 0, 1), "olay": m["refs"]["E"][g["ref"]]}
                              for g in e.get("gunduz_baglantisi") or [] if isinstance(g, dict) and g.get("ref") in E]
    for k in e["kisiler"] + e["onem_sinyali"]:
        k["anahtar"] = m["refs"]["P"].get(k["person"]) if k["person"] else None
    e["onem_sinyali"] = [{**s, "person": s.get("person") if s.get("person") in P else None,
                          "agirlik": _f(s.get("agirlik"), 0, 1)} for s in e.get("onem_sinyali") or [] if isinstance(s, dict)]
    return e, m


def ingest(conn) -> dict:
    batch = Batch(conn, "dream", ruya.raw_dir(), IMPORTER)
    conn.execute("DELETE FROM dream")
    conn.execute("DELETE FROM dream_eval")
    dec = ruya.effective_decisions()
    st = {"ruya": 0, "hatirlamiyorum": 0, "degerlendirme": 0, "kisi_onerisi": 0}
    for d in ruya.dreams():
        eid = batch.event(d["xid"], "dream", d["first_at"], day=d["day"], time_quality="day",
                          title="rüya" if d["recalled"] else "rüya yok",
                          payload={"recalled": d["recalled"], "parca": len(d["parts"]),
                                   "kaynak": sorted({p["src"] for p in d["parts"]})})
        conn.execute("INSERT INTO dream VALUES (?,?,?,?,?,?)", (d["xid"], eid, d["day"], d["recalled"], d["text"],
                                                                json.dumps(d["parts"], ensure_ascii=False)))
        st["ruya" if d["recalled"] else "hatirlamiyorum"] += 1
        keys = sorted(ruya.jobs_dir().glob(ruya.job_key(d) + "*.json"), key=lambda p: p.stat().st_mtime)
        got = next((x for x in (load_eval(p.name[:-5]) for p in reversed(keys) if not p.name.endswith(".meta.json")) if x), None)
        if not got:
            continue
        e, m = got
        du = e.get("duygu") or {}
        conn.execute("INSERT INTO dream_eval VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            d["xid"], m.get("key", ruya.job_key(d)), e.get("model"), m["context_until"], m["partial"],
            du.get("ana"), _f(du.get("valence"), -2, 2, int), _f(du.get("yogunluk"), 0, 3, int),
            *(json.dumps(e.get(k) or [], ensure_ascii=False) for k in
              ("temalar", "yerler", "kisiler", "gunduz_baglantisi", "onem_sinyali")),
            e.get("dusunce_izi"), json.dumps(e, ensure_ascii=False)))
        st["degerlendirme"] += 1
        for k in e["kisiler"]:
            if not k["anahtar"]:
                continue
            pid = conn.execute("SELECT person_id FROM person_alias WHERE source_kind=? AND alias=?",
                               k["anahtar"]).fetchone()
            v = dec.get((d["xid"], *k["anahtar"]))
            if v == "hayir" or not pid:
                continue
            batch.link_person(eid, pid[0], "dream", 1.0 if v == "evet" else min(k["guven"] or 0.3, 0.6))
            st["kisi_onerisi"] += 1
    return st
