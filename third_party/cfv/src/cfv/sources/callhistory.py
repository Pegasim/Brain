"""iPhone arama geçmişi (CallHistory.storedata, ZCALLRECORD) → 'call' event'leri.

Yalnız ŞİFRELİ yedekte bulunur (HomeDomain/Library/CallHistoryDB/). Bir kayıt = bir arama:
telefon, FaceTime ve CallKit kullanan uygulamaların (WhatsApp, Telegram, …) aramaları.

Kişi: numara E.164'e çevrilir (contacts.e164), sonra kişi ('phone', numara) ya da ('whatsapp', jid) takma adıyla
aranır; ikisi de yoksa ('phone', numara) takma adıyla yeni kişi açılır. E-posta tanıtıcısı (FaceTime) → ('email', …).

ÇİFT KAYIT KURALI (WhatsApp): iOS, CallKit üzerinden yapılan WhatsApp aramalarını da CallHistory'ye yazar; aynı
arama whatsapp kaynağında zaten `type='call'` mesajı olarak var. Sağlayıcısı WhatsApp olan bir kayıt, ±60 sn
içinde bir WhatsApp arama olayı varsa ve
  - kişisi çözüldüyse: o olayın karşı tarafı/göndereni aynı kişi ise,
  - kişisi çözülemediyse (opak tanıtıcı): pencerede TEK WhatsApp araması varsa
çift sayılır ve olay üretilmez (stats['whatsapp_duplicates']). Eşi bulunamayan WhatsApp araması normal 'call'
olayı olur (payload.provider='whatsapp'). Bu yüzden SOURCES'ta whatsapp'tan SONRA koşar; whatsapp yeni bir
snapshot'la yeniden alınınca bu kaynak da yeniden alınmalı (`ingest all` sırası bunu sağlar).

BİRLEŞİM: iOS eski aramaları zamanla siler. Yol verilmezse bütün tamamlanmış `extract-*` görüntüleri okunur ve
ZUNIQUE_ID'ye göre birleştirilir (aynı kayıt birden çok görüntüde varsa en yenisi geçerli); yeni görüntüde olmayan
eski arama böylece DB'den düşmez. Batch'in ham yolu en yeni görüntüdür.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from cfv.ingest import DONE_MARK, MARK_SINCE, Batch, drop_other_snapshots, open_ro, raw, snapshot_date, snapshot_of
from cfv.sources.contacts import e164, whatsapp_jid
from cfv.timeutil import from_coredata, iso

IMPORTER = "cfv.sources.callhistory@1"
STORE = ("HomeDomain", "Library", "CallHistoryDB", "CallHistory.storedata")  # en yeni extract-<tarih>/ altında

WA_WINDOW = dt.timedelta(seconds=60)
WA_PROVIDERS = {"net.whatsapp.WhatsApp", "net.whatsapp.WhatsAppSMB"}
PROVIDER = {"com.apple.Telephony": "phone", "com.apple.FaceTime": "facetime",
            "net.whatsapp.WhatsApp": "whatsapp", "net.whatsapp.WhatsAppSMB": "whatsapp",
            "ph.telegra.Telegraph": "telegram", "com.google.Tachyon": "google-meet",
            "com.viber": "viber", "com.skype.skype": "skype"}
# ZCALL_CATEGORY: 1 ses, 2 görüntü. ZHANDLE_TYPE: 1 genel (opak), 2 telefon numarası, 3 e-posta.
CATEGORY = {1: "audio", 2: "video"}
HANDLE_PHONE, HANDLE_EMAIL = 2, 3
ISO_CC = {"tr": "90", "us": "1", "gb": "44", "de": "49", "nl": "31", "fr": "33"}


def _text(v) -> str | None:
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    return v.strip() if isinstance(v, str) and v.strip() else None


def _owner(conn, kind: str, alias: str) -> int | None:
    row = conn.execute("SELECT person_id FROM person_alias WHERE source_kind=? AND alias=?",
                       (kind, alias)).fetchone()
    return row[0] if row else None


def wa_calls_near(conn, t: dt.datetime) -> list[set[int]]:
    """t ±60 sn içindeki WhatsApp arama olaylarının kişi kümeleri (karşı taraf + gönderen)."""
    rows = conn.execute(
        """SELECT e.id, ep.person_id FROM event e JOIN source s ON s.id = e.source_id
           LEFT JOIN event_person ep ON ep.event_id = e.id AND ep.role IN ('counterpart', 'sender')
           WHERE s.kind = 'whatsapp' AND e.kind = 'whatsapp' AND json_extract(e.payload, '$.type') = 'call'
             AND e.start_utc BETWEEN ? AND ?""",
        (iso(t - WA_WINDOW), iso(t + WA_WINDOW))).fetchall()
    calls: dict[int, set[int]] = {}
    for eid, pid in rows:
        calls.setdefault(eid, set())
        if pid is not None:
            calls[eid].add(pid)
    return list(calls.values())


def snapshots(root: Path | None = None) -> list[Path]:
    """Arama veritabanı olan bütün tamamlanmış extract-* görüntüleri, eskiden yeniye."""
    root = root or raw("iphone")
    return [d.joinpath(*STORE) for d in sorted(root.glob("extract-*"), key=snapshot_date)
            if d.is_dir() and ((d / DONE_MARK).exists() or snapshot_date(d) < MARK_SINCE)
            and d.joinpath(*STORE).exists()]


COLS = """Z_PK, ZUNIQUE_ID, ZDATE, ZDURATION, ZORIGINATED, ZANSWERED, ZCALL_CATEGORY, ZHANDLE_TYPE, ZADDRESS,
          ZNAME, ZSERVICE_PROVIDER, ZISO_COUNTRY_CODE"""


def ingest(conn, path: Path | list[Path] | None = None) -> dict:
    paths = [path] if isinstance(path, Path) else path or snapshots()
    if not paths:
        raise FileNotFoundError(raw("iphone", "extract-*", *STORE))
    records: dict[str, dict] = {}
    for p in paths:  # eskiden yeniye: yeni görüntü aynı kaydın üstüne yazar
        for r in open_ro(p).execute(f"SELECT {COLS} FROM ZCALLRECORD"):
            records[r["ZUNIQUE_ID"] or f"{snapshot_of(p)}/pk:{r['Z_PK']}"] = dict(r)
    latest = paths[-1]
    batch = Batch(conn, "callhistory", latest, IMPORTER, snapshot=snapshot_of(latest))
    drop_other_snapshots(conn, "callhistory", batch.source_id)
    stats = {"snapshots": len(paths), "calls": 0, "missed": 0, "outgoing": 0, "incoming": 0,
             "whatsapp_duplicates": 0, "whatsapp_unmatched": 0, "unresolved_handles": 0, "new_persons": 0}

    for ext, r in sorted(records.items(), key=lambda kv: kv[1]["ZDATE"] or 0):
        t = from_coredata(r["ZDATE"])
        if t is None:
            continue
        provider_id = r["ZSERVICE_PROVIDER"] or "com.apple.Telephony"
        provider = PROVIDER.get(provider_id, provider_id)
        address = _text(r["ZADDRESS"])
        name = _text(r["ZNAME"])
        cc = ISO_CC.get((r["ZISO_COUNTRY_CODE"] or "").lower(), "90")

        # --- kişi adayları ---
        cand: list[tuple[str, str]] = []
        number = None
        if address and address.endswith("@s.whatsapp.net"):
            cand = [("whatsapp", address)]
            number = "+" + address.split("@")[0]
        elif address and (r["ZHANDLE_TYPE"] == HANDLE_EMAIL or "@" in address):
            cand = [("email", address.lower())]
        elif address:
            number = e164(address, cc)
            if number:
                cand = [("phone", number), ("whatsapp", whatsapp_jid(number))]
        owners = {a: _owner(conn, *a) for a in cand}
        pids = {p for p in owners.values() if p is not None}

        # --- WhatsApp çift kaydı ---
        if provider_id in WA_PROVIDERS:
            near = wa_calls_near(conn, t)
            dup = any(pids & c for c in near) if pids else len(near) == 1
            if dup:
                stats["whatsapp_duplicates"] += 1
                continue
            stats["whatsapp_unmatched"] += 1

        pid = next((owners[a] for a in cand if owners[a] is not None), None)
        if pid is None and cand:
            pid = batch.person(cand[0][1], name or number or cand[0][1], source_kind=cand[0][0])
            stats["new_persons"] += 1
        if not cand:
            stats["unresolved_handles"] += 1

        outgoing = bool(r["ZORIGINATED"])
        answered = bool(r["ZANSWERED"])
        direction = "outgoing" if outgoing else "incoming" if answered else "missed"
        stats[direction] += 1
        dur = float(r["ZDURATION"] or 0)
        who = name or number or address or "bilinmeyen"
        label = {"outgoing": "giden arama", "incoming": "gelen arama", "missed": "cevapsız arama"}[direction]
        eid = batch.event(
            ext, "call", t, end=t + dt.timedelta(seconds=dur) if dur else None,
            title=f"{label}: {who}",
            payload={"direction": direction, "answered": answered, "duration_s": round(dur),
                     "provider": provider, "media": CATEGORY.get(r["ZCALL_CATEGORY"]),
                     "number": number, "handle": None if number else address})
        if pid is not None:
            batch.link_person(eid, pid, "counterpart")
        stats["calls"] += 1
    return stats
