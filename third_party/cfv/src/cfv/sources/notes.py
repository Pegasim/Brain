"""iCloud Notlar (NoteStore.sqlite) → 'note' event'leri + "Ödevler" notlarından 'todo' event'leri.

Ödev notu biçimi (Tuna, 2023 sonundan beri; en yeni hafta en üstte):
    **46**            ← kalın ISO hafta numarası (yanında etiket olabilir: "44 sınavlar")
    **salı**          ← kalın gün adı (pzt, salı, çarş, perş, cuma, cmt, pazar; kısaltmalar serbest)
    **PROJE**         ← (ops.) kalın büyük harf bölüm başlığı
    [x] yapılacak     ← checkbox / madde / düz satır
Yıl, not başlığından ("Ödevler 2025-2" → 2025) gelir; hafta numarası aşağı inerken
birden büyürse (1 → 52) önceki yıla geçilmiş sayılır.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from cfv.ingest import Batch, drop_other_snapshots, iphone_extract, open_ro, snapshot_of
from cfv.sources import notes_proto
from cfv.timeutil import DEFAULT_TZ, TR_DAYS, from_coredata, iso_week_monday

IMPORTER = "cfv.sources.notes@1"
DEFAULT_DB = ("AppDomainGroup-group.com.apple.notes", "NoteStore.sqlite")  # en yeni extract-<tarih>/ altında

TODO_TITLE = re.compile(r"^\s*ödevler\s+(\d{4})", re.I)
WEEK_RE = re.compile(r"^(\d{1,2})(?:\s+(.*))?$")

# Önek → gün indeksi (sıra önemli: 'pazartesi' 'pazar'dan önce)
_DAY_PREFIXES = [
    ("pazartesi", 0), ("pzt", 0), ("pts", 0),
    ("salı", 1), ("sali", 1), ("sal", 1),
    ("çarşamba", 2), ("çarş", 2), ("çaşr", 2), ("çar", 2), ("carsamba", 2), ("cars", 2), ("car", 2),
    ("perşembe", 3), ("perş", 3), ("persembe", 3), ("pers", 3), ("per", 3),
    ("cuma", 4),
    ("cumartesi", 5), ("cmt", 5), ("cts", 5),
    ("pazar", 6), ("pzr", 6), ("paz", 6),
]


def tr_lower(s: str) -> str:
    return s.replace("I", "ı").replace("İ", "i").lower()


def parse_day(text: str) -> int | None:
    """'cuma :-(' → 4, 'pzt-salı-çaşr' → 0, 'perşembe' → 3; gün değilse None."""
    t = tr_lower(text).strip()
    first = re.split(r"[\s\-–,:/()]+", t, maxsplit=1)[0] if t else ""
    if not first or len(t) > 24:
        return None
    for pre, idx in _DAY_PREFIXES:          # önce tam eşleşme ('pazar' ≠ 'pazartesi')
        if first == pre:
            return idx
    for pre, idx in _DAY_PREFIXES:          # sonra kısaltma: 'perşe' → perşembe
        if len(first) >= 3 and pre.startswith(first) and len(pre) - len(first) <= 6:
            return idx
    return None


@dataclass
class Todo:
    year: int
    week: int
    week_label: str | None
    day: int | None
    section: str | None
    text: str
    done: bool | None
    style: str
    order: int

    @property
    def date(self) -> dt.date:
        monday = iso_week_monday(self.year, self.week)
        return monday + dt.timedelta(days=self.day or 0)


@dataclass
class WeekBlock:
    year: int
    week: int
    label: str | None
    todos: list[Todo] = field(default_factory=list)


def parse_todo_note(lines: list[notes_proto.Line], base_year: int) -> list[WeekBlock]:
    weeks: list[WeekBlock] = []
    year, cur, day, section = base_year, None, None, None
    order = 0
    for ln in lines:
        text = ln.text.strip()
        if not text:
            continue
        is_header = ln.bold and ln.style not in ("checkbox",)
        if is_header:
            m = WEEK_RE.match(text)
            if m and 1 <= int(m.group(1)) <= 53:
                w = int(m.group(1))
                if cur is not None:
                    if w > cur.week + 2 and cur.week <= 4 and w >= 48:
                        year -= 1           # yıl dönümü (1 → 52)
                    elif w > cur.week:
                        m = None            # sıra bozuk → hafta değil, düz metin
                if m:
                    cur = WeekBlock(year, w, (m.group(2) or "").strip() or None)
                    weeks.append(cur)
                    day, section = None, None
                    continue
            d = parse_day(text)
            if d is not None and cur is not None:
                day, section = d, None
                continue
            if cur is not None:
                section = text
                continue
        elif cur is not None and ln.style == "body":
            d = parse_day(text)
            if d is not None and len(text) <= 12:   # kalın olmayan yalın gün başlığı
                day, section = d, None
                continue
        if cur is None:
            continue
        order += 1
        cur.todos.append(Todo(cur.year, cur.week, cur.label, day, section, text,
                              ln.checked if ln.style == "checkbox" else None, ln.style, order))
    return weeks


def _local_midnight(d: dt.date) -> dt.datetime:
    return dt.datetime.combine(d, dt.time(0, 0), DEFAULT_TZ)


def ingest(conn, db_path: Path | None = None) -> dict:
    db_path = db_path or iphone_extract().joinpath(*DEFAULT_DB)
    src = open_ro(db_path)
    batch = Batch(conn, "apple-notes", db_path, IMPORTER, snapshot=snapshot_of(db_path))
    drop_other_snapshots(conn, "apple-notes", batch.source_id)
    stats = {"notes": 0, "todo_notes": 0, "weeks": 0, "todos": 0, "todos_done": 0, "empty": 0}

    rows = src.execute("""
        SELECT n.Z_PK, n.ZIDENTIFIER, n.ZTITLE1 title, n.ZCREATIONDATE3 created,
               n.ZMODIFICATIONDATE1 modified, f.ZTITLE2 folder, d.ZDATA data
        FROM ZICCLOUDSYNCINGOBJECT n
        JOIN ZICNOTEDATA d ON d.Z_PK = n.ZNOTEDATA
        LEFT JOIN ZICCLOUDSYNCINGOBJECT f ON f.Z_PK = n.ZFOLDER
        WHERE coalesce(n.ZMARKEDFORDELETION, 0) = 0""").fetchall()

    for r in rows:
        if not r["data"]:
            stats["empty"] += 1
            continue
        lines = notes_proto.decode(r["data"])
        md = notes_proto.to_markdown(lines)
        created = from_coredata(r["created"]) or from_coredata(r["modified"])
        modified = from_coredata(r["modified"])
        if created is None:
            stats["empty"] += 1
            continue
        title = r["title"] or (lines[0].text if lines else None)
        m = TODO_TITLE.match(title or "")
        batch.event(r["ZIDENTIFIER"] or str(r["Z_PK"]), "note", created, end=modified,
                    title=title, body=md,
                    payload={"folder": r["folder"], "modified": modified, "is_todo_note": bool(m)})
        stats["notes"] += 1
        if not m:
            continue

        stats["todo_notes"] += 1
        seen: dict[str, int] = {}  # aynı hafta numarası notta iki kez geçebilir
        for wb in parse_todo_note(lines, int(m.group(1))):
            stats["weeks"] += 1
            for t in wb.todos:
                key = f"{wb.year}-W{wb.week:02d}-{t.day}-{t.text}"
                seen[key] = seen.get(key, 0) + 1
                ext = hashlib.blake2b(f"{r['ZIDENTIFIER']}|{key}|{seen[key]}".encode(),
                                      digest_size=10).hexdigest()
                d = t.date
                eid = batch.event(
                    ext, "todo", _local_midnight(d), time_quality="day" if t.day is not None else "week",
                    title=t.text, body=t.section, day=d.isoformat(),
                    payload={"done": t.done, "year": wb.year, "week": wb.week,
                             "week_label": wb.label,
                             "weekday": TR_DAYS[t.day] if t.day is not None else None,
                             "section": t.section, "note": title, "style": t.style,
                             "order": t.order})
                batch.tag(eid, "done" if t.done else ("open" if t.done is False else None),
                          f"section:{t.section}" if t.section else None)
                stats["todos"] += 1
                stats["todos_done"] += bool(t.done)
    return stats
