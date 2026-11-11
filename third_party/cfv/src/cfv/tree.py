"""Tarih bazlı, insan için gezilebilir klasör ağacı (DB'den türetilir, elle düzenlenmez).

    timeline/
      2025/
        README.md                     yıl özeti
        09-Eylül/
          README.md                   ay özeti (haftalar + sayımlar)
          W38 (15-21)/
            README.md                 gün gün: takvim, ödevler, notlar, saat verisi, foto, WhatsApp özeti
            whatsapp.md               haftanın mesajları (gün > sohbet)
            notlar/<başlık>.md        o hafta oluşturulan notlar
            fotolar/<tarih_saat>_<ad> → ~/Pictures/icloud-sync/... (sembolik link, kopya değil)

Ay sınırını aşan haftalar iki ayda da görünür; her ay klasöründe yalnızca o ayın günleri olur.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

from cfv import db
from cfv.timeutil import TR_DAYS, TR_MONTHS

MARKER = ".cfv-generated"
TODO_NOTE_RE = re.compile(r"^\s*ödevler\s+\d{4}", re.I)


def _safe(name: str, maxlen: int = 80) -> str:
    name = re.sub(r"[/\\\x00-\x1f]", "-", name).strip(". ") or "adsız"
    return name[:maxlen]


def _local(start_utc: str, off: int | None) -> dt.datetime:
    t = dt.datetime.strptime(start_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone(dt.timedelta(minutes=off if off is not None else 180)))


def week_dir(root: Path, d: dt.date, week_days: dict[tuple, list[dt.date]]) -> Path:
    y, w, _ = d.isocalendar()
    days = week_days[(d.year, d.month, y, w)]
    label = f"W{w:02d} ({days[0].day:02d}-{days[-1].day:02d})"
    return root / str(d.year) / f"{d.month:02d}-{TR_MONTHS[d.month - 1]}" / label


def _report_map() -> dict[dt.date, Path]:
    """Her gün → onu kapsayan en derin (saat parçası olmayan) rapor (src/compact üretir)."""
    best: dict[dt.date, tuple[int, Path]] = {}
    root = db.data_dir() / "processed" / "reports"
    for f in root.rglob("index.md"):
        m = re.search(r"^range:\s*(\S+)\s*(?:→\s*(\S+))?\s*$", f.read_text(), re.M)
        if not m:
            continue
        d, b = dt.date.fromisoformat(m.group(1)), dt.date.fromisoformat(m.group(2) or m.group(1))
        depth = len(f.relative_to(root).parts)
        while d <= b:
            if d not in best or depth > best[d][0]:
                best[d] = (depth, f)
            d += dt.timedelta(1)
    return {d: f for d, (_, f) in best.items()}


def build(conn, out: Path | None = None) -> dict:
    out = out or db.data_dir() / "processed" / "timeline"
    report_of = _report_map()
    if out.exists():
        if not (out / MARKER).exists():
            raise RuntimeError(f"{out} cfv tarafından üretilmemiş; üzerine yazmıyorum")
        shutil.rmtree(out)  # tamamen türetilmiş çıktı: baştan üret
    out.mkdir(parents=True)
    (out / MARKER).write_text("Bu klasör `cfv tree` ile üretilir; elle düzenleme kaybolur.\n")

    conn.row_factory = None
    rows = conn.execute("""
        SELECT e.id, e.kind, e.start_utc, e.end_utc, e.tz_offset_min, e.local_date, e.time_quality,
               e.title, e.body, e.payload
        FROM event e WHERE e.kind NOT IN (%s) ORDER BY e.local_date, e.start_utc""" % ",".join(
            f"'{k}'" for k in db.PRIVATE_KINDS)).fetchall()
    media = defaultdict(list)
    for eid, path, role in conn.execute(
            "SELECT event_id, path, role FROM media WHERE role IN ('original','adjusted')"):
        media[eid].append((role, path))

    cal_photos = dict(conn.execute(
        "SELECT b, count(*) FROM event_link WHERE kind = 'photo_at' GROUP BY b").fetchall())

    by_day: dict[dt.date, list] = defaultdict(list)
    for r in rows:
        by_day[dt.date.fromisoformat(r[5])].append(r)

    week_days: dict[tuple, list[dt.date]] = defaultdict(list)
    for d in sorted(by_day):
        y, w, _ = d.isocalendar()
        week_days[(d.year, d.month, y, w)].append(d)
    # haftanın o aydaki tüm günleri (etiket için ilk-son gün)
    for key in list(week_days):
        yr, mo, iy, iw = key
        mon = dt.date.fromisocalendar(iy, iw, 1)
        week_days[key] = [mon + dt.timedelta(i) for i in range(7)
                          if (mon + dt.timedelta(i)).month == mo and (mon + dt.timedelta(i)).year == yr]

    stats = Counter()
    weeks: dict[Path, list[dt.date]] = defaultdict(list)
    for d in sorted(by_day):
        weeks[week_dir(out, d, week_days)].append(d)

    month_summary: dict[Path, list[tuple[str, Counter]]] = defaultdict(list)
    for wdir, days in weeks.items():
        wdir.mkdir(parents=True, exist_ok=True)
        stats["weeks"] += 1
        lines = [f"# {wdir.parent.parent.name} · {wdir.parent.name} · {wdir.name}", ""]
        wa_lines: list[str] = []
        wcount = Counter()

        # haftaya ait (günü belli olmayan) ödevler
        week_todos = [r for d in days for r in by_day[d] if r[1] == "todo" and r[6] == "week"]
        if week_todos:
            lines += ["## Haftanın ödevleri", ""]
            lines += _todo_lines(week_todos)
            lines.append("")

        for d in days:
            evs = by_day[d]
            k = Counter(r[1] for r in evs)
            wcount.update(k)
            lines += [f"## {TR_DAYS[d.weekday()]} {d.day} {TR_MONTHS[d.month - 1]} {d.year}", ""]
            rep = report_of.get(d)
            if rep:
                lvl = re.search(r"^level:\s*(.+?)\s*$", rep.read_text(), re.M)
                lines += [f"📝 [{(lvl.group(1) if lvl else 'rapor').capitalize()} raporu]({rep})", ""]

            cal = [r for r in evs if r[1] == "calendar"]
            if cal:
                lines.append("**Takvim**")
                for r in cal:
                    p = json.loads(r[9] or "{}")
                    when = "tüm gün" if p.get("all_day") else _local(r[2], r[4]).strftime("%H:%M")
                    loc = f" — {p['location']}" if p.get("location") else ""
                    ph = f" · 📷 {cal_photos[r[0]]}" if r[0] in cal_photos else ""
                    lines.append(f"- {when} {r[7] or '(başlıksız)'}{loc}{ph}")
                lines.append("")

            todos = [r for r in evs if r[1] == "todo" and r[6] != "week"]
            if todos:
                lines.append("**Ödevler**")
                lines += _todo_lines(todos)
                lines.append("")

            notes = [r for r in evs if r[1] == "note"]
            if notes:
                lines.append("**Notlar**")
                ndir = wdir / "notlar"
                for r in notes:
                    if TODO_NOTE_RE.match(r[7] or ""):
                        lines.append(f"- {r[7]} (ödev defteri başladı)")
                        continue
                    ndir.mkdir(exist_ok=True)
                    fn = f"{_safe(r[7] or 'not')}.md"
                    (ndir / fn).write_text(r[8] or "", encoding="utf-8")
                    lines.append(f"- [{r[7]}](notlar/{fn.replace(' ', '%20')})")
                    stats["note_files"] += 1
                lines.append("")

            health = [r for r in evs if r[1] in ("zepp_day", "sleep", "workout")]
            if health:
                parts = []
                for r in health:
                    p = json.loads(r[9] or "{}")
                    if r[1] == "zepp_day":
                        s = r[7]
                        if p.get("rest_hr"):
                            s += f", dinlenik nabız {p['rest_hr']}"
                        if p.get("pai_daily"):
                            s += f", PAI +{p['pai_daily']}"
                        parts.append(s)
                    elif r[1] == "sleep":
                        parts.append(f"{r[7]} ({_local(r[2], r[4]):%H:%M}–{_local(r[3], r[4]):%H:%M})")
                    else:
                        parts.append(f"{r[7]} ({_local(r[2], r[4]):%H:%M})")
                lines += ["**Saat (Zepp)**", *[f"- {x}" for x in parts], ""]

            pics = [r for r in evs if r[1] in ("photo", "video")]
            if pics:
                people = Counter()
                places = Counter()
                linked = 0
                fdir = wdir / "fotolar"
                for r in pics:
                    p = json.loads(r[9] or "{}")
                    people.update(p.get("people") or [])
                    if p.get("moment"):
                        places[p["moment"]] += 1
                    files = sorted(media.get(r[0], []))  # adjusted < original: düzenlenmiş varsa onu
                    if files:
                        fdir.mkdir(exist_ok=True)
                        target = files[0][1]
                        name = f"{_local(r[2], r[4]):%Y-%m-%d_%H%M%S}_{Path(target).name}"
                        link = fdir / name
                        if not link.exists():
                            os.symlink(target, link)
                            linked += 1
                n_photo, n_video = k.get("photo", 0), k.get("video", 0)
                s = f"{n_photo} foto" + (f", {n_video} video" if n_video else "")
                if linked < len(pics):
                    s += f" ({linked} dosya bağlı)"
                stats["photo_links"] += linked
                lines.append(f"**Fotoğraflar** — {s} → [fotolar/](fotolar/)")
                if people:
                    lines.append("- Kişiler: " + ", ".join(f"{n} ({c})" for n, c in people.most_common(12)))
                if places:
                    lines.append("- Yer: " + ", ".join(p for p, _ in places.most_common(5)))
                lines.append("")

            msgs = [r for r in evs if r[1] == "whatsapp"]
            if msgs:
                chats = Counter(r[7] for r in msgs)
                top = ", ".join(f"{c} ({n})" for c, n in chats.most_common(8))
                lines += [f"**WhatsApp** — {len(msgs)} mesaj, {len(chats)} sohbet: {top} → "
                          f"[whatsapp.md](whatsapp.md#{d.isoformat()})", ""]
                wa_lines += [f"## {d.isoformat()} {TR_DAYS[d.weekday()]}", ""]
                by_chat = defaultdict(list)
                for r in msgs:
                    by_chat[r[7]].append(r)
                for chat, rs in sorted(by_chat.items(), key=lambda x: -len(x[1])):
                    wa_lines += [f"### {chat}", ""]
                    for r in rs:
                        p = json.loads(r[9] or "{}")
                        who = p.get("sender") or "?"
                        text = (r[8] or "").replace("\n", " ⏎ ")
                        wa_lines.append(f"- {_local(r[2], r[4]):%H:%M} **{who}**: {text}")
                    wa_lines.append("")

        if wa_lines:
            (wdir / "whatsapp.md").write_text(f"# WhatsApp · {wdir.name}\n\n" + "\n".join(wa_lines),
                                              encoding="utf-8")
        (wdir / "README.md").write_text("\n".join(lines), encoding="utf-8")
        month_summary[wdir.parent].append((wdir.name, wcount))

    # ay ve yıl özetleri
    year_summary: dict[Path, list[tuple[str, Counter]]] = defaultdict(list)
    for mdir, wks in sorted(month_summary.items()):
        tot = Counter()
        lines = [f"# {mdir.parent.name} · {mdir.name}", "", "| Hafta | Foto | Video | WhatsApp | Takvim | Ödev | Not |",
                 "|---|---|---|---|---|---|---|"]
        for name, c in sorted(wks):
            tot.update(c)
            lines.append(f"| [{name}]({name.replace(' ', '%20')}/README.md) | {c['photo']} | {c['video']} | "
                         f"{c['whatsapp']} | {c['calendar']} | {c['todo']} | {c['note']} |")
        (mdir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        year_summary[mdir.parent].append((mdir.name, tot))
    for ydir, months in sorted(year_summary.items()):
        lines = [f"# {ydir.name}", "", "| Ay | Foto | Video | WhatsApp | Takvim | Ödev | Not |",
                 "|---|---|---|---|---|---|---|"]
        for name, c in sorted(months):
            lines.append(f"| [{name}]({name}/README.md) | {c['photo']} | {c['video']} | "
                         f"{c['whatsapp']} | {c['calendar']} | {c['todo']} | {c['note']} |")
        (ydir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        stats["years"] += 1
    stats["out"] = str(out)
    return dict(stats)


def _todo_lines(todos) -> list[str]:
    out, section = [], None
    for r in sorted(todos, key=lambda r: json.loads(r[9] or "{}").get("order", 0)):
        p = json.loads(r[9] or "{}")
        if p.get("section") != section:
            section = p.get("section")
            if section:
                out.append(f"- *{section}*")
        box = "[x]" if p.get("done") else ("[ ]" if p.get("done") is False else "-")
        indent = "  " if section else ""
        out.append(f"{indent}- {box} {r[7]}" if box != "-" else f"{indent}- {r[7]}")
    return out
