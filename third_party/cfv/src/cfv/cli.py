"""`cfv` komut satırı."""

from __future__ import annotations

import argparse
import importlib
import json
import time
from pathlib import Path

from cfv import db

# ad → modül (cfv.sources.<modül>.ingest(conn))
SOURCES = {
    "photos": "photos",
    "notes": "notes",
    "calendar-ios": "calendar_ios",
    "calendar-ics": "calendar_ics",
    "calendar-google": "calendar_gcal",
    "reports": "reports",
    "contacts": "contacts",  # gmail/whatsapp'tan önce: kişi takma adları onlarla birleşsin
    "gmail": "gmail",
    "whatsapp": "whatsapp",
    "callhistory": "callhistory",  # whatsapp'tan sonra: CallKit WhatsApp aramaları çift sayılmasın
    "zepp": "zepp",
    "health": "health",  # zepp'ten sonra: zepp.py'nin kapsadığı günler Sağlık'tan tekrar alınmasın
    "dream": "dream",
}


def cmd_ingest(args):
    names = list(SOURCES) if args.source == ["all"] else args.source
    conn = db.connect(args.db)
    conn.executescript(db.schema_sql())
    for name in names:
        mod = importlib.import_module(f"cfv.sources.{SOURCES[name]}")
        t = time.time()
        try:
            with conn:
                stats = mod.ingest(conn)
        except FileNotFoundError as e:
            print(f"[{name}] ATLANDI — ham veri yok: {e}")
            continue
        print(f"[{name}] {time.time() - t:.1f} sn  {json.dumps(stats, ensure_ascii=False)}")
    from cfv import link
    print(f"[link] {json.dumps(link.run_all(conn), ensure_ascii=False)}")


def cmd_tree(args):
    from cfv import tree
    stats = tree.build(db.connect(args.db), args.out)
    print(json.dumps(stats, ensure_ascii=False))


def cmd_search(args):
    from cfv import search
    conn = db.connect(args.db)
    if args.by_week:
        for w in search.weeks(conn, args.query, limit=args.limit):
            print(f"{w['week']}  {w['hits']:4d} eşleşme  {w['kinds']}")
            for h in w["samples"]:
                print(f"      {h['local_date']} [{h['kind']}] {h['snippet']}")
    else:
        for h in search.search(conn, args.query, limit=args.limit, kinds=args.kind):
            title = h["title"] or ""
            extra = "" if h["snippet"] in ("", title) else f" — {h['snippet']}"
            print(f"{h['local_date']} [{h['kind']}] {title}{extra}")


def cmd_chart(args):
    from cfv import chart
    print(json.dumps(chart.build(db.connect(args.db), args.out), ensure_ascii=False))


def cmd_sync(args):
    if args.target == "gmail":
        from cfv import gmail_sync
        gmail_sync.run(ingest=not args.no_ingest)
        print(gmail_sync.log_path().read_text().rstrip().splitlines()[-1])
        return
    from cfv import gcal_sync
    if args.status:
        state = gcal_sync._read_json(gcal_sync.out_dir() / "state.json", {})
        print(json.dumps(state, ensure_ascii=False, indent=1))
        print(f"log: {gcal_sync.log_path()}")
        return
    gcal_sync.run(force_full=args.full, allow_mass_removal=args.allow_mass_removal,
                  ingest=not args.no_ingest)
    print(gcal_sync.log_path().read_text().rstrip().splitlines()[-1])


def cmd_status(args):
    """Kaynak başına sayı/son olay, tazelik, son kapanan gün; --buyume N: son N günün büyümesi."""
    from akis import buyume, gun, sayim, tazelik
    path = args.db or db.default_db_path()
    if args.buyume is not None:
        print(buyume.table(args.buyume))
        return
    counts, last = sayim.counts(path), sayim.last_events(path)
    snaps = {k: (snap, imp) for k, _, snap, imp in sayim.sources(path)}
    print(f"{'kaynak':16} {'olay':>8}  {'son olay':10}  {'snapshot':20}  içe aktarıldı")
    for k in sorted(counts, key=lambda k: -counts[k]):
        snap, imp = snaps.get(k, (None, None))
        print(f"{k:16} {counts[k]:>8}  {(last.get(k) or '-')[:10]:10}  {(snap or '-')[:20]:20}  {(imp or '-')[:16]}")
    print(f"{'toplam':16} {sum(counts.values()):>8}")
    print(f"foto türevleri: {sayim.photo_derivs()}")
    g = gun.summary()
    rev = ", ".join(f"{d} (r{r})" for d, r in g["revize_son"][-10:]) or "-"
    print(f"son kapanan gün: {g['son_kapanan'] or '-'} · son 7 günde geç veriyle revize: {rev}")
    print()
    print(tazelik.report(tazelik.evaluate(path)))
    rs = buyume.rows()
    if rs:
        print()
        print(buyume.table(7))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cfv", description="Central Finite Vault")
    parser.add_argument("--db", type=Path, default=None, help="Varsayılan: data/processed/cfv.sqlite")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="Zaman çizelgesi veritabanını oluştur/güncelle")

    p = sub.add_parser("ingest", help="Kaynak(lar)ı içe aktar")
    p.add_argument("source", nargs="+", choices=[*SOURCES, "all"])

    p = sub.add_parser("tree", help="Tarih bazlı klasör ağacını üret (sene/ay/hafta)")
    p.add_argument("--out", type=Path, default=None, help="Varsayılan: data/processed/timeline")

    p = sub.add_parser("search", help="Tam metin arama")
    p.add_argument("query")
    p.add_argument("--kind", action="append", help="note, todo, whatsapp, photo, calendar ...")
    p.add_argument("--by-week", action="store_true", help="Sonuçları haftalara grupla")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("chart", help="Haftalık değişken grafiği (data/processed/chart.html)")
    p.add_argument("--out", type=Path, default=None)

    p = sub.add_parser("sync", help="Google Takvim / Gmail'i data/raw/'a senkronla (salt-okur)")
    p.add_argument("target", choices=["gcal", "gmail"])
    p.add_argument("--full", action="store_true", help="Değişiklik kontrolünü atla, tam çek")
    p.add_argument("--no-ingest", action="store_true", help="cfv.sqlite/timeline'ı güncelleme")
    p.add_argument("--allow-mass-removal", action="store_true",
                   help="Olayların %%20'sinden fazlası silinmiş görünse de yaz")
    p.add_argument("--status", action="store_true", help="Son senkron durumunu göster")

    p = sub.add_parser("status", help="Kaynak sayıları, tazelik, gün kapanışı; --buyume N: son N gün büyüme")
    p.add_argument("--buyume", type=int, nargs="?", const=14, default=None, metavar="N")

    p = sub.add_parser("ruya", help="Rüya günlüğü: tur | durum | goster | karar | geri | yeniden")
    p.add_argument("rest", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)
    if args.cmd == "init-db":
        print(db.init_db(args.db))
    elif args.cmd == "ingest":
        cmd_ingest(args)
    elif args.cmd == "tree":
        cmd_tree(args)
    elif args.cmd == "search":
        cmd_search(args)
    elif args.cmd == "chart":
        cmd_chart(args)
    elif args.cmd == "sync":
        cmd_sync(args)
    elif args.cmd == "ruya":
        from cfv import ruya
        ruya.cli(args.rest)
    elif args.cmd == "status":
        cmd_status(args)


if __name__ == "__main__":
    main()
