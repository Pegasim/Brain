"""SENTETİK örnek cfv.sqlite — site geliştirmek için. İçindeki herkes ve her şey uydurmadır.

    PYTHONPATH=src/site .venv/bin/python -m cfv_site.synth <çıktı.sqlite> [--scale 1.0] [--seed 7]

Telefonlar 90000… ile başlar (gerçek hat değil), e-postalar @ornek.test. Mesaj metinleri anlamsız
kelime dizileridir. Bilerek eşleşme adayları üretir: aynı kişinin kaynaklarda farklı yazılışı,
aynı ilk isimli iki farklı kişi vb.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import random
from pathlib import Path

from cfv import db, link

FIRST = ["Aslı", "Deniz", "Ece", "Mert", "Can", "Zeynep", "Emre", "Elif", "Kaan", "Selin", "Burak",
         "Ceren", "Arda", "İrem", "Onur", "Defne", "Cem", "Melis", "Efe", "Duru", "Berk", "Ilgın",
         "Kerem", "Naz", "Ozan", "Pelin", "Umut", "Yağmur", "Barış", "Ezgi", "Tolga", "Şule", "Doruk",
         "Gizem", "Alp", "Öykü", "Sarp", "Nehir", "Tan", "Çağla"]
LAST = ["Yılmaz", "Kaya", "Demir", "Şahin", "Çelik", "Aydın", "Öztürk", "Arslan", "Doğan", "Koç",
        "Kurt", "Özdemir", "Polat", "Erdem", "Aksoy", "Tekin", "Güneş", "Bulut", "Yıldız", "Uçar"]
WORDS = ("lorem ipsum dolor sit amet tamam olur yarın bakarız şimdi geliyorum harika evet hayır "
         "belki saat kaçta nerede buluşalım kahve ders proje sınav film maç konser hafta sonu "
         "bence güzel fikir haha süper teşekkürler görüşürüz").split()
CAL_TITLES = ["Kahve", "Proje toplantısı", "Ders: Lineer Cebir", "Ders: Olasılık", "Doktor randevusu",
              "Konser", "Doğum günü", "Akşam yemeği", "Sinema", "Spor salonu", "Sunum provası",
              "Kulüp toplantısı", "Mülakat", "Halı saha", "Kitap kulübü"]
TRIPS = ["Kamp: Kaz Dağları", "Gezi: İzmir", "Gezi: Ayvalık", "Kamp: Abant", "Gezi: Eskişehir",
         "Tatil: Kaş", "Gezi: Kapadokya"]
NOTE_TITLES = ["Alışveriş listesi", "Okunacaklar", "Fikirler", "Ders notları", "Gezi planı",
               "Yapılacaklar", "Film listesi", "Toplantı notu", "Hediye fikirleri", "Bütçe"]
GROUPS = ["Lise 2019", "Ev arkadaşları", "Proje ekibi", "Kamp 2024", "Aile", "Kulüp yönetim"]

START = dt.date(2023, 1, 1)
END = dt.date(2026, 10, 31)
TZ = 180  # İstanbul, dk


def _iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_dt(day: dt.date, hour: float) -> dt.datetime:
    """Yerel gün + saat → UTC datetime."""
    loc = dt.datetime.combine(day, dt.time()) + dt.timedelta(hours=hour)
    return loc - dt.timedelta(minutes=TZ)


def _words(rng, n):
    return " ".join(rng.choice(WORDS) for _ in range(n))


def _activity(h: dict, day: dt.date) -> float:
    """Kişinin o günkü yakınlığı: etkin aralıkta yumuşak çan eğrisi."""
    if day < h["from"] or day > h["to"]:
        return 0.0
    span = (h["to"] - h["from"]).days or 1
    x = (day - h["from"]).days / span
    return h["intensity"] * (0.35 + 0.65 * math.sin(math.pi * x)) * (1.3 if day.weekday() >= 5 else 1)


def generate(out: Path, scale: float = 1.0, seed: int = 7) -> dict:
    rng = random.Random(seed)
    if out.exists():
        raise FileExistsError(out)
    db.init_db(out)
    conn = db.connect(out)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('synthetic', '1')")
    now = _iso(dt.datetime.now(dt.timezone.utc))
    src = {}
    for kind in ("iphone-photos", "whatsapp", "calendar-google", "apple-notes", "contacts", "gmail"):
        src[kind] = conn.execute(
            "INSERT INTO source (kind, raw_path, snapshot, imported_at, importer) VALUES (?,?,?,?,?)",
            (kind, f"raw/synthetic/{kind}", "2026-11-01", now, "cfv_site.synth@1")).lastrowid

    person_ids: dict[tuple[str, str], int] = {}

    def person(sk, alias, name):
        k = (sk, alias)
        if k not in person_ids:
            pid = conn.execute("INSERT INTO person (display_name) VALUES (?)", (name,)).lastrowid
            conn.execute("INSERT INTO person_alias VALUES (?,?,?,?)", (pid, sk, alias, name))
            person_ids[k] = pid
        return person_ids[k]

    # --- insanlar
    used = set()
    humans = []
    days_total = (END - START).days
    for i in range(45):
        while True:
            fn, ln = rng.choice(FIRST), rng.choice(LAST)
            if (fn, ln) not in used:
                break
        used.add((fn, ln))
        a = START + dt.timedelta(days=rng.randint(-200, days_total - 120))
        b = a + dt.timedelta(days=rng.randint(90, 1100))
        humans.append({
            "first": fn, "last": ln, "from": max(a, START), "to": min(b, END),
            "intensity": rng.choice([0.2, 0.5, 1, 2, 4, 8]) * scale,
            "photo": rng.random(), "phone": f"90000{rng.randint(1000000, 9999999)}",
            "in_wa": rng.random() < 0.9, "in_contacts": rng.random() < 0.7,
            "in_photos": rng.random() < 0.6, "in_email": rng.random() < 0.35,
        })
    # bilerek: aynı ilk isimli iki farklı kişi (reddedilmesi gereken öneri)
    humans[1]["first"] = humans[0]["first"]
    humans[1]["in_photos"] = humans[0]["in_photos"] = True

    for h in humans:
        full = f"{h['first']} {h['last']}"
        h["ids"] = {}
        if h["in_contacts"]:
            h["ids"]["contacts"] = person("contacts", "+" + h["phone"], full)
        if h["in_wa"]:
            # rehberde varsa WhatsApp rehber adını gösterir; yoksa kendi koyduğu ad
            wa_name = full if h["in_contacts"] else rng.choice(
                [f"{h['first']} {h['last'][0]}.", h["first"], f"{h['first'].lower()} {h['last'].lower()}"])
            h["ids"]["whatsapp"] = person("whatsapp", f"{h['phone']}@s.whatsapp.net", wa_name)
        if h["in_photos"]:
            ph_name = rng.choice([h["first"], full, full])
            h["ids"]["iphone-photos"] = person("iphone-photos", f"SYN-{rng.getrandbits(64):016X}", ph_name)
        if h["in_email"]:
            addr = f"{db.fold_tr(h['first']).lower()}.{db.fold_tr(h['last']).lower()}@ornek.test"
            addr = addr.translate(str.maketrans("şğüöç", "sguoc"))
            h["ids"]["email"] = person("email", addr, rng.choice([full, addr]))

    ev_rows, ep_rows, media_rows = [], [], []
    next_id = [1]

    def event(kind, source, ext, start, *, end=None, title=None, body=None, payload=None,
              quality="exact", day=None):
        eid = next_id[0]
        next_id[0] += 1
        ld = day or (start + dt.timedelta(minutes=TZ)).date().isoformat()
        ev_rows.append((eid, src[source], f"{ext}#{eid}", kind, _iso(start), _iso(end) if end else None, TZ, ld,
                        quality, title, body, json.dumps(payload, ensure_ascii=False) if payload else None))
        return eid

    # --- WhatsApp: DM + gruplar
    groups = []
    for gname in GROUPS:
        members = rng.sample([h for h in humans if h["in_wa"]], rng.randint(4, 12))
        a = START + dt.timedelta(days=rng.randint(0, days_total - 200))
        groups.append({"name": gname, "jid": f"{rng.randint(10**11, 10**12)}-{rng.randint(10**9, 10**10)}@g.us",
                       "members": members, "from": a,
                       "to": min(END, a + dt.timedelta(days=rng.randint(150, 900))),
                       "intensity": rng.choice([1, 3, 6]) * scale})
    trips = []
    for t in TRIPS:
        d0 = START + dt.timedelta(days=rng.randint(30, days_total - 30))
        trips.append({"title": t, "from": d0, "days": rng.randint(2, 6),
                      "with": rng.sample(humans, rng.randint(2, 6))})

    day = START
    while day <= END:
        for h in humans:
            act = _activity(h, day)
            if not act:
                continue
            if h["in_wa"]:
                n = min(60, int(rng.expovariate(1 / act))) if rng.random() < min(1, act) else 0
                pid = h["ids"]["whatsapp"]
                jid = f"{h['phone']}@s.whatsapp.net"
                for j in range(n):
                    t = _local_dt(day, rng.uniform(8, 24))
                    me = rng.random() < 0.5
                    eid = event("whatsapp", "whatsapp", f"{jid}/{day}-{j}", t,
                                title=f"{h['first']} {h['last']}", body=_words(rng, rng.randint(1, 9)),
                                payload={"chat": f"{h['first']} {h['last']}", "chat_type": "dm",
                                         "chat_jid": jid, "from_me": me, "sender": "ben" if me else h["first"],
                                         "type": "text"})
                    ep_rows.append((eid, pid, "counterpart", 1.0))
        for g in groups:
            if g["from"] <= day <= g["to"] and rng.random() < 0.6:
                for j in range(int(rng.expovariate(1 / g["intensity"]))):
                    m = rng.choice(g["members"] + [None])
                    t = _local_dt(day, rng.uniform(9, 24))
                    eid = event("whatsapp", "whatsapp", f"{g['jid']}/{day}-{j}", t, title=g["name"],
                                body=_words(rng, rng.randint(1, 12)),
                                payload={"chat": g["name"], "chat_type": "group", "chat_jid": g["jid"],
                                         "from_me": m is None, "sender": "ben" if m is None else m["first"],
                                         "type": "text"})
                    if m is not None:
                        ep_rows.append((eid, m["ids"]["whatsapp"], "sender", 1.0))
        # --- fotolar
        trip = next((t for t in trips if t["from"] <= day < t["from"] + dt.timedelta(days=t["days"])), None)
        lam = (2.5 if day.weekday() < 5 else 5) * scale * (6 if trip else 1)
        n_photos = int(rng.expovariate(1 / lam)) if rng.random() < 0.55 or trip else 0
        around = [h for h in humans if h["in_photos"] and _activity(h, day) and rng.random() < h["photo"]]
        if trip:
            around += [h for h in trip["with"] if h["in_photos"]]
        for j in range(n_photos):
            t = _local_dt(day, rng.uniform(9, 23))
            video = rng.random() < 0.08
            eid = event("video" if video else "photo", "iphone-photos", f"SYN-{day}-{j}", t,
                        payload={"album": trip["title"] if trip else None})
            media_rows.append((eid, f"/nonexistent/synthetic/{day:%Y/%m}/IMG_{eid:06d}.{'MOV' if video else 'HEIC'}",
                               "original", f"IMG_{eid:06d}", "video/quicktime" if video else "image/heic"))
            for h in rng.sample(around, min(len(around), rng.choice([0, 0, 1, 1, 2, 3]))):
                ep_rows.append((eid, h["ids"]["iphone-photos"], "in_photo", 1.0))
        # --- takvim
        for _ in range(int(rng.random() < 0.55) + int(rng.random() < 0.2)):
            h0 = rng.choice([9, 10, 13, 15, 18, 19, 20])
            dur = rng.choice([1, 1, 1.5, 2, 3])
            t0, t1 = _local_dt(day, h0), _local_dt(day, h0 + dur)
            eid = event("calendar", "calendar-google", f"cal-{day}-{h0}", t0, end=t1,
                        title=rng.choice(CAL_TITLES), payload={"calendar": "kişisel"})
            for h in rng.sample(humans, rng.choice([0, 0, 1, 2])):
                if "email" in h["ids"]:
                    ep_rows.append((eid, h["ids"]["email"], "attendee", 1.0))
        for t in trips:
            if t["from"] == day:
                s = _local_dt(day, 0)
                eid = event("calendar", "calendar-google", f"trip-{day}", s,
                            end=s + dt.timedelta(days=t["days"]), title=t["title"], quality="day",
                            day=day.isoformat(), payload={"calendar": "kişisel", "all_day": True})
                for h in t["with"]:
                    if "email" in h["ids"]:
                        ep_rows.append((eid, h["ids"]["email"], "attendee", 1.0))
        # --- notlar
        if rng.random() < 0.25:
            eid = event("note", "apple-notes", f"note-{day}", _local_dt(day, rng.uniform(8, 23)),
                        title=rng.choice(NOTE_TITLES), body=_words(rng, rng.randint(8, 60)))
            if rng.random() < 0.3:
                h = rng.choice(humans)
                if h["ids"]:
                    ep_rows.append((eid, next(iter(h["ids"].values())), "mentioned", 1.0))
        day += dt.timedelta(days=1)

    with conn:
        conn.executemany(
            "INSERT INTO event (id, source_id, external_id, kind, start_utc, end_utc, tz_offset_min, "
            "local_date, time_quality, title, body, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ev_rows)
        conn.executemany("INSERT OR IGNORE INTO event_person VALUES (?,?,?,?)", ep_rows)
        conn.executemany("INSERT INTO media (event_id, path, role, original_filename, mime) "
                         "VALUES (?,?,?,?,?)", media_rows)
    stats = link.run_all(conn)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode = DELETE")  # salt-okur (ProtectSystem) açılışta -shm gerekmesin
    conn.close()
    return {"events": len(ev_rows), "event_person": len(ep_rows), "media": len(media_rows),
            "people": len(person_ids), "humans": len(humans), **stats}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", type=Path)
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    print(json.dumps(generate(a.out, a.scale, a.seed), ensure_ascii=False))


if __name__ == "__main__":
    main()
