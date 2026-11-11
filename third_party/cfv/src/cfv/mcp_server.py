"""CFV MCP sunucusu (stdio, SALT-OKUR). Claude/ajanlar zaman çizelgesini buradan sorgular.

Araçlar:
  when       — "ne zaman X ile uğraştım?": eşleşmeleri haftalara toplar
  search     — tam metin arama (tür/tarih/sohbet/kişi süzgeçli)
  timeline   — tarih aralığındaki ham event'ler
  digest     — dönem özeti (Instagram postu vb. için)
  week_page  — bir tarihin haftalık klasör özeti (README.md)
  people     — kişi ara (foto etiketleri + WhatsApp)
  photos     — dönem/kişi için foto dosya yolları (Claude ancak seçileni açar)
Vektör (anlamsal) arama eklenecek: `semantic_search` aynı event id'lerini döndürecek.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from cfv import db, search as S
from cfv.tree import week_dir  # noqa: F401  (yol mantığı tek yerde)
from cfv.timeutil import TR_MONTHS

mcp = MCPServer("Brain", instructions=(
    "Kişisel zaman çizelgesi (fotolar+kişiler, iCloud Notlar ve haftalık ödev listeleri, "
    "WhatsApp, takvim, Zepp saat verisi). Tarihler yerel gün 'YYYY-MM-DD'. Türkçe kelimelerde "
    "kök kullan (ödevlerimi → ödev); arama önek eşleşmelidir. 'Ne zaman' sorularında önce `when`, "
    "sonra bulunan hafta için `week_page` veya `digest` kullan. Veri kişiseldir: yalnızca soruyla "
    "ilgili kısmı aktar."))


def _conn() -> sqlite3.Connection:
    path = db.default_db_path()
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
def when(query: str, kinds: list[str] | None = None, limit: int = 10) -> list[dict]:
    """Bir konunun hangi haftalarda geçtiğini bul (en yoğun haftalar önce, örnek satırlarla).
    kinds: todo, note, whatsapp, calendar, photo, video, zepp_day, sleep, workout."""
    return S.weeks(_conn(), query, kinds=kinds, limit=limit)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
def search(query: str, kinds: list[str] | None = None, start: str | None = None,
           end: str | None = None, chat: str | None = None, person: str | None = None,
           limit: int = 30, order: str = "rank") -> list[dict]:
    """Tam metin arama. order='time' kronolojik verir. chat: WhatsApp sohbet adı parçası;
    person: kişi adı parçası (fotoda olan / mesajlaşılan)."""
    return S.search(_conn(), query, kinds=kinds, start=start, end=end, chat=chat, person=person,
                    limit=min(limit, 200), order=order)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
def timeline(start: str, end: str, kinds: list[str] | None = None, limit: int = 300) -> list[dict]:
    """Tarih aralığındaki tüm event'ler (yerel gün, dahil), zaman sırasıyla."""
    return S.timeline(_conn(), start, end, kinds=kinds, limit=min(limit, 2000))


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
def digest(start: str, end: str) -> dict:
    """Dönem özeti: sayımlar, takvim, ödevler, fotolardaki kişiler, yerler, en çok yazışılan
    sohbetler, ortalama adım/uyku."""
    return S.digest(_conn(), start, end)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
def week_page(date: str) -> str:
    """Verilen günün haftalık klasör sayfası (data/processed/timeline/.../README.md)."""
    d = dt.date.fromisoformat(date)
    root = db.data_dir() / "processed" / "timeline" / str(d.year) / f"{d.month:02d}-{TR_MONTHS[d.month - 1]}"
    w = d.isocalendar()[1]
    for p in sorted(root.glob(f"W{w:02d} *")) if root.exists() else []:
        readme = p / "README.md"
        if readme.exists():
            return f"[{readme}]\n\n" + readme.read_text(encoding="utf-8")
    return "Bu hafta için sayfa yok (o hafta veri olmayabilir ya da `cfv tree` çalıştırılmadı)."


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
def people(name: str, limit: int = 20) -> list[dict]:
    """Kişi ara: ad parçası → kişi, kaynak, foto/mesaj sayısı, ilk-son tarih."""
    rows = _conn().execute("""
        SELECT p.id, p.display_name, group_concat(DISTINCT a.source_kind),
               sum(ep.role='in_photo'), sum(ep.role IN ('counterpart','sender')),
               min(e.local_date), max(e.local_date)
        FROM person p JOIN person_alias a ON a.person_id=p.id
        LEFT JOIN event_person ep ON ep.person_id=p.id LEFT JOIN event e ON e.id=ep.event_id
        WHERE p.display_name LIKE ? GROUP BY p.id ORDER BY count(ep.event_id) DESC LIMIT ?""",
        (f"%{name}%", limit)).fetchall()
    return [{"id": r[0], "name": r[1], "sources": r[2], "photos": r[3], "messages": r[4],
             "first": r[5], "last": r[6]} for r in rows]


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
def photos(start: str, end: str, person: str | None = None, limit: int = 60) -> list[dict]:
    """Dönemdeki fotoğraf/videoların dosya yolları (+kişiler, yer). Görmek için seçilen yolu aç."""
    where, args = ["e.kind IN ('photo','video')", "e.local_date BETWEEN ? AND ?"], [start, end]
    if person:
        where.append("""e.id IN (SELECT ep.event_id FROM event_person ep JOIN person p ON p.id=ep.person_id
                        WHERE p.display_name LIKE ?)""")
        args.append(f"%{person}%")
    rows = _conn().execute(f"""
        SELECT e.local_date, e.start_utc, e.kind, json_extract(e.payload,'$.people'),
               json_extract(e.payload,'$.moment'),
               (SELECT path FROM media m WHERE m.event_id=e.id ORDER BY m.role='adjusted' DESC,
                       m.role='original' DESC LIMIT 1)
        FROM event e WHERE {' AND '.join(where)} ORDER BY e.start_utc LIMIT ?""",
        (*args, min(limit, 500))).fetchall()
    return [{"date": r[0], "utc": r[1], "kind": r[2], "people": r[3], "place": r[4], "path": r[5]}
            for r in rows]


def main():
    if not Path(db.default_db_path()).exists():
        raise SystemExit("cfv.sqlite yok: önce `uv run cfv ingest all`")
    mcp.run()


if __name__ == "__main__":
    main()
