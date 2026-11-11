"""iPhone Rehberi (AddressBook.sqlitedb) → kişi takma adları (olay üretmez).

Her rehber kaydı bir `person` olur ve üç tür takma ad alır:
  ('contacts', ABPerson.guid)            rehber kaydının kendisi
  ('phone', '+900001112233')             E.164 numara (arama geçmişi / SMS eşleştirmesi için)
  ('whatsapp', '900001112233@s.whatsapp.net')   aynı numaranın WhatsApp JID'i
  ('email', 'ad@example.com')              küçük harf e-posta (gmail okuyucusuyla aynı biçim)

Eşleştirme kesin bilgiyle sınırlı: numara ya da e-posta zaten başka bir kaynağın kişisine aitse
(ör. WhatsApp sohbet kişisi, Gmail göndereni) rehber kaydı O kişiye bağlanır. Takma adlar farklı kişilere
dağılmışsa kişiler BİRLEŞTİRİLMEZ (olay bağları değişmesin); çakışma olarak sayılır, ileri eşleştirme
başka adımın işi. Birden çok rehber kaydında geçen numara (ev telefonu, ortak hat) ve "eski numara"
etiketli numaralar belirsiz olduğu için takma ad olmaz.
Kişiler silinmez (ingest.py kuralı); tekrar koşmak aynı sonucu verir.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from cfv.ingest import Batch, iphone_extract, open_ro, snapshot_of

IMPORTER = "cfv.sources.contacts@1"
ABOOK = ("HomeDomain", "Library", "AddressBook", "AddressBook.sqlitedb")  # en yeni extract-<tarih>/ altında

PROP_PHONE, PROP_EMAIL = 3, 4
OLD_LABEL = re.compile(r"\b(old|eski)\b", re.I)
DEFAULT_CC = "90"


def e164(value, cc: str = DEFAULT_CC) -> str | None:
    """Rehberdeki numarayı E.164'e çevir. Emin olunamayan biçim (kısa kod, yerel 7 hane) → None."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if s.startswith("+"):
        return "+" + digits if 8 <= len(digits) <= 15 and digits[0] != "0" else None
    if digits.startswith("00") and 8 <= len(digits) - 2 <= 15 and digits[2] != "0":  # 00 49 … (ülke kodu 0'la başlamaz)
        return "+" + digits[2:]
    if len(digits) == 11 and digits[0] == "0":       # 0 5xx xxx xx xx
        return f"+{cc}{digits[1:]}"
    if len(digits) == 10 and digits[0] in "23458":   # 5xx xxx xx xx (TR alan/mobil kodu)
        return f"+{cc}{digits}"
    if len(digits) == 12 and digits.startswith(cc):  # 90 5xx … ('+' unutulmuş)
        return "+" + digits
    return None


def whatsapp_jid(number: str) -> str:
    return number.lstrip("+") + "@s.whatsapp.net"


def display_name(r) -> str | None:
    full = " ".join(p.strip() for p in (r["First"], r["Middle"], r["Last"]) if p and p.strip())
    return full or (r["Organization"] or "").strip() or (r["Nickname"] or "").strip() or None


def ingest(conn, path: Path | None = None) -> dict:
    path = path or iphone_extract().joinpath(*ABOOK)
    src = open_ro(path)
    batch = Batch(conn, "contacts", path, IMPORTER, snapshot=snapshot_of(path))
    stats = {"contacts": 0, "new_persons": 0, "matched_existing": 0, "conflicts": 0,
             "phones": 0, "emails": 0, "shared_numbers": 0, "old_numbers": 0, "unparsed_numbers": 0}

    labels = {r[0]: r[1] for r in src.execute("SELECT ROWID, value FROM ABMultiValueLabel")}
    phones: dict[int, list[str]] = {}
    emails: dict[int, list[str]] = {}
    for r in src.execute("SELECT record_id, property, label, value FROM ABMultiValue "
                         "WHERE property IN (?, ?) ORDER BY record_id, identifier",
                         (PROP_PHONE, PROP_EMAIL)):
        if r["property"] == PROP_PHONE:
            if OLD_LABEL.search(labels.get(r["label"]) or ""):
                stats["old_numbers"] += 1
                continue
            n = e164(r["value"])
            if n is None:
                stats["unparsed_numbers"] += 1
                continue
            lst = phones.setdefault(r["record_id"], [])
            if n not in lst:
                lst.append(n)
        else:
            e = (r["value"] or "").strip().lower() if isinstance(r["value"], str) else ""
            if "@" in e and e not in emails.setdefault(r["record_id"], []):
                emails[r["record_id"]].append(e)

    owners = Counter(n for lst in phones.values() for n in lst)
    shared = {n for n, k in owners.items() if k > 1}
    stats["shared_numbers"] = len(shared)

    def owner(kind: str, alias: str) -> int | None:
        row = conn.execute("SELECT person_id FROM person_alias WHERE source_kind=? AND alias=?",
                           (kind, alias)).fetchone()
        return row[0] if row else None

    for r in src.execute("SELECT ROWID, guid, First, Middle, Last, Organization, Nickname FROM ABPerson"):
        stats["contacts"] += 1
        nums = [n for n in phones.get(r["ROWID"], []) if n not in shared]
        mails = emails.get(r["ROWID"], [])
        name = display_name(r) or (nums[0] if nums else mails[0] if mails else f"rehber {r['ROWID']}")
        key = r["guid"] or f"rowid:{r['ROWID']}"
        aliases = [("contacts", key)]
        for n in nums:
            aliases += [("whatsapp", whatsapp_jid(n)), ("phone", n)]
        aliases += [("email", e) for e in mails]

        found = {a: owner(*a) for a in aliases}
        pids = [p for p in found.values() if p is not None]
        if found[("contacts", key)] is not None:
            pid = found[("contacts", key)]
        elif pids:
            pid = pids[0]  # sıra: whatsapp, phone, email
            stats["matched_existing"] += 1
        else:
            pid = conn.execute("INSERT INTO person (display_name) VALUES (?)", (name,)).lastrowid
            stats["new_persons"] += 1
        if any(p != pid for p in pids):
            stats["conflicts"] += 1

        for (kind, alias), p in found.items():
            if p is None:
                conn.execute("INSERT INTO person_alias (person_id, source_kind, alias, name) VALUES (?,?,?,?)",
                             (pid, kind, alias, name))
        stats["phones"] += len(nums)
        stats["emails"] += len(mails)
    return stats
