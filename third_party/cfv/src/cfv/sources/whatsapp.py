"""WhatsApp (Business) ChatStorage.sqlite → 'whatsapp' event'leri (mesaj başına bir event)."""

from __future__ import annotations

from pathlib import Path

from cfv.ingest import Batch, drop_other_snapshots, iphone_extract, open_ro, snapshot_of
from cfv.timeutil import from_coredata

IMPORTER = "cfv.sources.whatsapp@1"
SHARED = ("AppDomainGroup-group.net.whatsapp.WhatsAppSMB.shared",)  # en yeni extract-<tarih>/ altında

SESSION_TYPE = {0: "dm", 1: "group", 2: "broadcast", 3: "status", 4: "community"}
MSG_TYPE = {0: "text", 1: "image", 2: "video", 3: "audio", 4: "contact", 5: "location",
            6: "system", 7: "link", 8: "document", 10: "call", 11: "gif", 14: "deleted",
            15: "sticker", 46: "poll"}
PLACEHOLDER = {"image": "[foto]", "video": "[video]", "audio": "[ses]", "contact": "[kişi kartı]",
               "location": "[konum]", "document": "[belge]", "sticker": "[çıkartma]",
               "gif": "[gif]", "deleted": "[silinmiş mesaj]", "call": "[arama]", "poll": "[anket]"}
SKIP_SESSIONS = {3}  # durum (status) güncellemeleri


def ingest(conn, shared: Path | None = None) -> dict:
    shared = shared or iphone_extract().joinpath(*SHARED)
    db_path = shared / "ChatStorage.sqlite"
    src = open_ro(db_path)
    batch = Batch(conn, "whatsapp", db_path, IMPORTER, snapshot=snapshot_of(db_path))
    drop_other_snapshots(conn, "whatsapp", batch.source_id)
    stats = {"messages": 0, "skipped_system": 0, "chats": 0, "with_media": 0}

    sessions = {r["Z_PK"]: r for r in src.execute(
        "SELECT Z_PK, ZSESSIONTYPE, ZPARTNERNAME, ZCONTACTJID FROM ZWACHATSESSION")}
    members = {r["Z_PK"]: r for r in src.execute(
        "SELECT Z_PK, ZMEMBERJID, ZCONTACTNAME, ZFIRSTNAME FROM ZWAGROUPMEMBER")}
    pushnames = {r["ZJID"]: r["ZPUSHNAME"] for r in src.execute(
        "SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME")}
    stats["chats"] = sum(1 for s in sessions.values() if s["ZSESSIONTYPE"] not in SKIP_SESSIONS)

    def member_name(m):
        return m["ZCONTACTNAME"] or m["ZFIRSTNAME"] or pushnames.get(m["ZMEMBERJID"]) or m["ZMEMBERJID"]

    seen: set[str] = set()
    for r in src.execute("""
            SELECT m.Z_PK, m.ZSTANZAID, m.ZCHATSESSION, m.ZISFROMME, m.ZMESSAGETYPE, m.ZMESSAGEDATE,
                   m.ZTEXT, m.ZGROUPMEMBER, m.ZSTARRED, mi.ZMEDIALOCALPATH, mi.ZTITLE mtitle,
                   mi.ZLATITUDE, mi.ZLONGITUDE
            FROM ZWAMESSAGE m LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
            ORDER BY m.ZMESSAGEDATE"""):
        s = sessions.get(r["ZCHATSESSION"])
        if s is None or s["ZSESSIONTYPE"] in SKIP_SESSIONS:
            continue
        mtype = MSG_TYPE.get(r["ZMESSAGETYPE"], f"type{r['ZMESSAGETYPE']}")
        text = r["ZTEXT"]
        if mtype == "system" and not text:
            stats["skipped_system"] += 1
            continue
        t = from_coredata(r["ZMESSAGEDATE"])
        if t is None:
            continue
        chat = s["ZPARTNERNAME"] or s["ZCONTACTJID"]
        ctype = SESSION_TYPE.get(s["ZSESSIONTYPE"], "other")
        from_me = bool(r["ZISFROMME"])
        sender_jid = sender = None
        if from_me:
            sender = "ben"
        elif ctype == "dm":
            sender_jid, sender = s["ZCONTACTJID"], chat
        elif r["ZGROUPMEMBER"] in members:
            mem = members[r["ZGROUPMEMBER"]]
            sender_jid, sender = mem["ZMEMBERJID"], member_name(mem)

        body = text or r["mtitle"] or PLACEHOLDER.get(mtype, f"[{mtype}]")
        if text and mtype not in ("text", "link"):
            body = f"{PLACEHOLDER.get(mtype, '')} {text}".strip()
        lat, lon = r["ZLATITUDE"], r["ZLONGITUDE"]
        if not lat and not lon:
            lat = lon = None
        ext = f"{s['ZCONTACTJID']}/{r['ZSTANZAID'] or r['Z_PK']}"
        if ext in seen:
            ext = f"{ext}#{r['Z_PK']}"
        seen.add(ext)
        eid = batch.event(
            ext, "whatsapp", t, title=chat, body=body,
            lat=lat, lon=lon,
            payload={"chat": chat, "chat_type": ctype, "chat_jid": s["ZCONTACTJID"],
                     "from_me": from_me, "sender": sender, "type": mtype,
                     "starred": bool(r["ZSTARRED"]) or None})
        stats["messages"] += 1

        # karşı taraf: DM'de sohbet kişisi, grupta mesajı atan
        if ctype == "dm" and s["ZCONTACTJID"]:
            batch.link_person(eid, batch.person(s["ZCONTACTJID"], chat), "counterpart")
        elif sender_jid:
            batch.link_person(eid, batch.person(sender_jid, sender), "sender")
        if r["ZMEDIALOCALPATH"]:
            p = shared / "Message" / r["ZMEDIALOCALPATH"]
            if p.exists():
                batch.media(eid, p, "attachment")
                stats["with_media"] += 1
    return stats
