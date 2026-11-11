"""iPhone Photos.sqlite → photo/video event'leri + kişiler + icloud-sync dosya eşlemesi.

Dosya eşleme (icloudpd adlandırması, photos ajanından):
  - ad = iCloud orijinal adı (ZADDITIONALASSETATTRIBUTES.ZORIGINALFILENAME), harf büyüklüğü farklı olabilir
  - çakışma: '<ad>-<iCloud orijinal boyutu>.<uzt>'  (≈ ZORIGINALFILESIZE)
  - düzenlenmiş: '<ad>-adjusted.<uzt>' (uzantı farklı olabilir), Live Photo videosu: '<ad>_HEVC.MOV'
  - '<dosya>.xmp' içinde xmp:CreateDate (tz'li) → çekim zamanı (±2 sn); ayrıca GPS, xmp:Rating 5 = favori,
    -1 = gizli/silinmiş (icloudpd 'reddedilmiş' olarak yazar)

Snapshot: YALNIZ en yeni data/raw/iphone/photos-db-*/Photos.sqlite kabul edilir; eski snapshot'ların
olayları silinir (yoksa her yeni snapshot'ta fotolar çift olurdu). Daha eski bir snapshot verilirse
reddedilir (yoksa yeni snapshot'ın olaylarını silerdi).
Photos.sqlite'taki hiçbir asset'e (ne asıl ne 'alt' olarak) eşleşmeyen ve XMP zamanı olan her dosya
XMP'den asgari olay olur (etiket 'icloud-only'; kişi/albüm yok): snapshot'tan sonra çekilenler de,
snapshot'tan sonra eklenmiş eski tarihli olanlar da. Yeni snapshot gelince dosya DB'ye eşleşir, asgari
olay kendiliğinden kalkar. XMP'siz eşleşmeyen dosyalar yalnız 'disk_unused' sayılır.
Etiketler: çöpteki asset 'deleted' (olay kalır, dosyası eşlenir); 'screenshot' = iOS bayrağı
ZISDETECTEDSCREENSHOT ya da alt tür 10 (WhatsApp vb. ile kaydedilen ekran görüntüleri de).
"""

from __future__ import annotations

import datetime as dt
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from cfv.ingest import Batch, latest_dir, open_ro, raw
from cfv.timeutil import from_coredata

IMPORTER = "cfv.sources.photos@1"
DEFAULT_DB = ("iphone", "photos-db-2026-11-01", "Photos.sqlite")  # en yeni snapshot yoksa
# Kütüphane: CFV_PHOTO_LIBRARY > sunucu arşivi (icloudpd günlük çeker) > laptop yolu
_LIB_CANDIDATES = [Path("/mnt/depo1/foto/icloud-sync"), Path.home() / "Pictures" / "icloud-sync"]
DEFAULT_LIBRARY = Path(os.environ["CFV_PHOTO_LIBRARY"]) if os.environ.get("CFV_PHOTO_LIBRARY") else next(
    (p for p in _LIB_CANDIDATES if p.is_dir()), _LIB_CANDIDATES[-1])
VIDEO_EXT = {"mov", "mp4", "m4v", "avi", "3gp", "mkv", "webm"}

SUBTYPE_TAG = {1: "panorama", 2: "live", 10: "screenshot", 100: "video-streamed",
               101: "slomo", 102: "timelapse", 103: "screen-recording"}
_XMP_DATE = re.compile(r"<xmp:CreateDate>([^<]+)</xmp:CreateDate>")


@dataclass
class DiskFile:
    path: Path
    stem: str       # eşleme anahtarı: küçük harf, son eksiz kök ad
    ext: str        # küçük harf, noktasız
    role: str       # original | adjusted | live
    size_hint: int | None  # çakışma son ekindeki orijinal boyut
    taken: dt.datetime | None
    used: bool = False
    lat: float | None = None
    lon: float | None = None
    favorite: bool = False
    size: int | None = None  # diskteki bayt
    hidden: bool = False     # XMP Rating -1: iCloud'da gizli ya da silinmiş


@dataclass
class _Asset:
    uuid: str
    eid: int
    taken: dt.datetime
    fname: str
    stem: str
    ext: str
    fsize: int | None
    video: bool
    width: int | None
    height: int | None
    duration: float | None


def _xmp_num(text: str, tag: str) -> float | None:
    m = re.search(rf"<{tag}>([-0-9.]+)</{tag}>", text)
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


def _xmp_info(p: Path) -> tuple[dt.datetime | None, float | None, float | None, float | None]:
    """XMP sidecar → (çekim zamanı, enlem, boylam, Rating). Rating: 5 favori, -1 gizli/silinmiş."""
    try:
        text = p.read_text(errors="ignore")
    except OSError:
        return None, None, None, None
    taken = None
    m = _XMP_DATE.search(text)
    if m:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S"):
            try:
                t = dt.datetime.strptime(m.group(1), fmt)
                taken = t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
                break
            except ValueError:
                continue
    lat, lon = _xmp_num(text, "exif:GPSLatitude"), _xmp_num(text, "exif:GPSLongitude")
    return taken, lat, lon, _xmp_num(text, "xmp:Rating")


def parse_name(name: str) -> tuple[str, str, str, int | None]:
    """'IMG_0787-5337221.JPG' → ('img_0787', 'jpg', 'original', 5337221)."""
    stem, _, ext = name.rpartition(".")
    role, size = "original", None
    if stem.endswith("-adjusted"):
        stem, role = stem[: -len("-adjusted")], "adjusted"
    elif stem.endswith("_HEVC") and ext.lower() == "mov":
        stem, role = stem[: -len("_HEVC")], "live"
    m = re.fullmatch(r"(.+)-(\d{4,})", stem)
    if m:
        stem, size = m.group(1), int(m.group(2))
    return stem.lower(), ext.lower(), role, size


def scan_library(root: Path) -> dict[str, list[DiskFile]]:
    index: dict[str, list[DiskFile]] = defaultdict(list)
    if not root.exists():
        return index
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]  # gizli klasörler (ör. kopya karantinası) atlanır
        for n in names:
            if n.endswith((".xmp", ".part")) or n.startswith("."):
                continue
            p = Path(dirpath) / n
            stem, ext, role, size = parse_name(n)
            xmp = p.with_name(n + ".xmp")
            taken, lat, lon, rating = _xmp_info(xmp) if xmp.exists() else (None, None, None, None)
            try:
                nbytes = p.stat().st_size
            except OSError:
                nbytes = None
            index[stem].append(DiskFile(p, stem, ext, role, size, taken, lat=lat, lon=lon,
                                        favorite=rating == 5, size=nbytes, hidden=rating == -1))
    return index


def latest_snapshot() -> Path:
    """data/raw/iphone/photos-db-<tarih>/Photos.sqlite içinden tamamlanmış (.tamam) en yenisi (yoksa DEFAULT_DB)."""
    snaps = [d for d in raw("iphone").glob("photos-db-*") if (d / "Photos.sqlite").exists()]
    d = latest_dir("photos-db", DEFAULT_DB[1]) if snaps else None
    return d / "Photos.sqlite" if d and (d / "Photos.sqlite").exists() else raw(*DEFAULT_DB)


EXT_ALIAS = {"jpeg": "jpg", "tif": "tiff"}
TIME_MAX_S = 36 * 3600   # ad eşleşmesinde en çok zaman farkı (aynı adlı başka asset olabilir)
RENAME_S = 2             # farklı uzantı / farklı ad eşleşmesi yalnız bu kadar yakın zamanda
SIZE_TOL = 4096          # eski --set-exif-datetime dosyayı en çok birkaç KB değiştirdi


def _norm_ext(ext: str) -> str:
    return EXT_ALIAS.get(ext, ext)


def _time_part(f: DiskFile, taken: dt.datetime) -> float | None:
    if f.taken is not None:
        d = abs((f.taken - taken).total_seconds())
        return None if d > TIME_MAX_S else d
    # XMP yok: klasör YYYY/MM ile çekim ayı uzaklığı (ay sınırında tz kayması olabilir)
    try:
        y, m = int(f.path.parent.parent.name), int(f.path.parent.name)
        return abs((y * 12 + m) - (taken.year * 12 + taken.month)) * 86400 * 20 + 3600
    except ValueError:
        return 10**6


def _score(a: _Asset, f: DiskFile, role: str) -> float | None:
    """Küçük = iyi; None = bu dosya bu asset'e bu rolde olamaz."""
    s = _time_part(f, a.taken)
    if s is None:
        return None
    if role == "original":
        if (f.ext in VIDEO_EXT) != a.video:
            return None
        if _norm_ext(f.ext) != _norm_ext(a.ext):
            # HEIC↔JPG gibi: yalnız XMP zamanı neredeyse aynıysa
            if f.taken is None or s > RENAME_S:
                return None
            s += 600
        if a.fsize and f.size is not None and abs(f.size - a.fsize) > SIZE_TOL:
            s += 30
    if f.size_hint is not None:
        if a.fsize and abs(f.size_hint - a.fsize) > 1024:
            s += 10**6
        # eşitlikte soneksiz asıl kazansın: '-<boyut>' çoğu zaman --set-exif-datetime kopyasıydı
        s += 0.5
    return s


def _assign(pairs: list, done: set, out: list, role: str, how: str):
    """Global açgözlü atama: en iyi puanlı çiftten başlar; sıra (puan, uuid, yol) ile deterministik."""
    pairs.sort(key=lambda x: (x[0], x[1].uuid, str(x[2].path)))
    for _, a, f in pairs:
        if a.uuid in done or f.used:
            continue
        f.used = True
        done.add(a.uuid)
        out.append((a, f, role, how))


def match_files(assets: list[_Asset], disk: dict[str, list[DiskFile]]) -> list[tuple]:
    """(asset, dosya, rol, nasıl) listesi. Önce ad, sonra (yalnız kalanlar için) zaman+boyut."""
    out: list[tuple] = []
    by_stem: dict[str, list[_Asset]] = defaultdict(list)
    for a in assets:
        by_stem[a.stem].append(a)
    for role in ("original", "adjusted", "live"):
        pairs = []
        for stem, group in by_stem.items():
            for f in disk.get(stem, ()):
                if f.role != role or f.used:
                    continue
                for a in group:
                    sc = _score(a, f, role)
                    if sc is not None:
                        pairs.append((sc, a, f))
        done: set = set()
        _assign(pairs, done, out, role, "ad")
        if role == "original":
            # adı değişmiş dosya: aynı sınıf, XMP zamanı ±2 sn, boyut ±4 KB, kullanılmamış
            left = [a for a in assets if a.uuid not in done and a.fsize]
            if left:
                by_sec: dict[int, list[DiskFile]] = defaultdict(list)
                for files in disk.values():
                    for f in files:
                        if f.role == "original" and not f.used and f.taken is not None and f.size is not None:
                            by_sec[int(f.taken.timestamp())].append(f)
                pairs = []
                for a in left:
                    t = int(a.taken.timestamp())
                    for sec in range(t - RENAME_S, t + RENAME_S + 1):
                        for f in by_sec.get(sec, ()):
                            d = abs((f.taken - a.taken).total_seconds())
                            if d <= RENAME_S and abs(f.size - a.fsize) <= SIZE_TOL and \
                                    (f.ext in VIDEO_EXT) == a.video:
                                pairs.append((d + 900, a, f))
                _assign(pairs, done, out, role, "zaman+boyut")
    return out


def ingest(conn, db_path: Path | None = None, library: Path | None = None) -> dict:
    latest = latest_snapshot()
    if db_path is None:
        db_path = latest
    elif Path(db_path).resolve() != latest.resolve():
        # ingest diğer snapshot'ların olaylarını siler: eski snapshot verilirse yenisinin olayları giderdi
        raise ValueError(f"photos: yalnız en yeni snapshot kabul edilir ({latest}), verilen: {db_path}")
    db_path = Path(db_path)
    library = library or DEFAULT_LIBRARY
    src = open_ro(db_path)
    batch = Batch(conn, "iphone-photos", db_path, IMPORTER, snapshot=db_path.parent.name[-10:])
    # eski snapshot'ların olayları (yeni snapshot aynı asset'leri yeniden getirir → çift olmasın)
    conn.execute("DELETE FROM event WHERE source_id IN (SELECT id FROM source WHERE kind=? AND id<>?)",
                 ("iphone-photos", batch.source_id))

    # --- kişiler (birleştirilmiş kişileri hedefe çöz) ---
    persons = {r["Z_PK"]: r for r in src.execute(
        "SELECT Z_PK, ZFULLNAME, ZDISPLAYNAME, ZPERSONUUID, ZMERGETARGETPERSON FROM ZPERSON")}

    def resolve(pk):
        seen = set()
        while pk in persons and persons[pk]["ZMERGETARGETPERSON"] and pk not in seen:
            seen.add(pk)
            pk = persons[pk]["ZMERGETARGETPERSON"]
        return pk

    faces: dict[int, set[int]] = defaultdict(set)
    for r in src.execute("SELECT ZASSETFORFACE a, ZPERSONFORFACE p FROM ZDETECTEDFACE "
                         "WHERE ZASSETFORFACE IS NOT NULL AND ZPERSONFORFACE IS NOT NULL"):
        pk = resolve(r["p"])
        if pk in persons and (persons[pk]["ZFULLNAME"] or "").strip():
            faces[r["a"]].add(pk)

    albums: dict[int, list[str]] = defaultdict(list)
    for r in src.execute(
            "SELECT j.Z_3ASSETS a, g.ZTITLE t FROM Z_30ASSETS j JOIN ZGENERICALBUM g ON g.Z_PK=j.Z_30ALBUMS "
            "WHERE g.ZKIND=2 AND g.ZTRASHEDSTATE=0 AND g.ZTITLE IS NOT NULL"):
        albums[r["a"]].append(r["t"])

    moments = {r["Z_PK"]: (r["ZTITLE"], r["ZSUBTITLE"]) for r in src.execute(
        "SELECT Z_PK, ZTITLE, ZSUBTITLE FROM ZMOMENT")}

    disk = scan_library(library)
    stats = defaultdict(int)
    by_stem: dict[str, list[tuple[int, dt.datetime, str, int | None]]] = defaultdict(list)
    assets: list[_Asset] = []

    # iOS ekran görüntüsü bayrağı (WhatsApp vb. ile kaydedilenler dahil); eski şemada yok → yalnız alt tür 10
    acols = {c[1] for c in src.execute("PRAGMA table_info(ZASSET)")}
    shot = "a.ZISDETECTEDSCREENSHOT" if "ZISDETECTEDSCREENSHOT" in acols else "NULL"
    # çöpteki (silinmiş) asset'ler de olay olur ('deleted'): yoksa arşivdeki dosyaları icloud-only geri gelirdi
    rows = src.execute(f"""
        SELECT a.Z_PK, a.ZUUID, a.ZKIND, a.ZKINDSUBTYPE, a.ZDATECREATED, a.ZLATITUDE, a.ZLONGITUDE,
               a.ZFAVORITE, a.ZHIDDEN, a.ZWIDTH, a.ZHEIGHT, a.ZDURATION, a.ZMOMENT, a.ZTRASHEDSTATE trashed,
               {shot} shot, a.ZUNIFORMTYPEIDENTIFIER uti,
               x.ZORIGINALFILENAME fname, x.ZORIGINALFILESIZE fsize, x.ZTIMEZONEOFFSET tzoff,
               x.ZTIMEZONENAME tzname, d.ZLONGDESCRIPTION descr
        FROM ZASSET a LEFT JOIN ZADDITIONALASSETATTRIBUTES x ON x.ZASSET = a.Z_PK
        LEFT JOIN ZASSETDESCRIPTION d ON d.Z_PK = x.ZASSETDESCRIPTION
        ORDER BY a.ZDATECREATED""").fetchall()

    for r in rows:
        taken = from_coredata(r["ZDATECREATED"])
        if taken is None:
            stats["no_date"] += 1
            continue
        off = int(r["tzoff"] // 60) if r["tzoff"] is not None else None
        lat, lon = r["ZLATITUDE"], r["ZLONGITUDE"]
        if lat is None or lat < -90 or (lat == 0 and lon == 0):
            lat = lon = None
        names = sorted({persons[pk]["ZFULLNAME"].strip() for pk in faces.get(r["Z_PK"], ())})
        mt, ms = moments.get(r["ZMOMENT"], (None, None))
        alb = albums.get(r["Z_PK"], [])
        body_parts = []
        if names:
            body_parts.append("Kişiler: " + ", ".join(names))
        if mt or ms:
            body_parts.append("Yer: " + " · ".join(x for x in (mt, ms) if x))
        if alb:
            body_parts.append("Albüm: " + ", ".join(alb))
        if isinstance(r["descr"], str) and r["descr"].strip():
            body_parts.append(r["descr"].strip())
        kind = "video" if r["ZKIND"] == 1 else "photo"
        eid = batch.event(
            r["ZUUID"], kind, taken, tz_offset_min=off, title=r["fname"],
            body="\n".join(body_parts) or None, lat=lat, lon=lon,
            payload={"moment": mt, "moment_sub": ms, "albums": alb or None, "people": names or None,
                     "uti": r["uti"], "orig_size": r["fsize"], "tz": r["tzname"],
                     "duration_s": r["ZDURATION"] or None})
        sub = SUBTYPE_TAG.get(r["ZKINDSUBTYPE"])
        batch.tag(eid, sub, "favorite" if r["ZFAVORITE"] else None,
                  "hidden" if r["ZHIDDEN"] else None, "deleted" if r["trashed"] else None,
                  "screenshot" if r["shot"] == 1 and sub != "screenshot" else None, *[f"album:{a}" for a in alb])
        if r["trashed"]:
            stats["deleted"] += 1
        for pk in faces.get(r["Z_PK"], ()):
            p = persons[pk]
            pid = batch.person(p["ZPERSONUUID"], p["ZFULLNAME"].strip())
            batch.link_person(eid, pid, "in_photo")

        # --- disk eşleme: önce tüm asset'ler toplanır, sonra global atama (sıradan bağımsız) ---
        if not r["fname"]:
            stats["no_filename"] += 1
            continue
        stem, ext, _, _ = parse_name(r["fname"])
        by_stem[stem].append((eid, taken, r["fname"], r["ZWIDTH"]))
        assets.append(_Asset(r["ZUUID"], eid, taken, r["fname"], stem, ext, r["fsize"], r["ZKIND"] == 1,
                             r["ZWIDTH"], r["ZHEIGHT"], r["ZDURATION"] or None))

    matched_orig = set()
    for a, f, role, how in match_files(assets, disk):
        batch.media(a.eid, f.path, role, original_filename=a.fname, width=a.width, height=a.height,
                    duration_s=a.duration)
        stats[f"file_{role}"] += 1
        if role == "original":
            matched_orig.add(a.uuid)
            if how != "ad":
                stats["file_original_by_time"] += 1
            elif _norm_ext(f.ext) != _norm_ext(a.ext):
                stats["file_original_other_ext"] += 1
    stats["matched"] = len(matched_orig)
    stats["unmatched"] = len(assets) - len(matched_orig)

    # --- ikinci geçiş: aynı adlı, aynı anda çekilmiş artık dosyalar = aynı asset'in diğer
    # sürümleri (farklı uzantı ya da boyut son ekiyle inmiş türev) → role 'alt'
    for stem, files in disk.items():
        for f in files:
            if f.used or f.taken is None or stem not in by_stem:
                continue
            eid, taken, fname, _ = min(by_stem[stem], key=lambda x: abs((x[1] - f.taken).total_seconds()))
            if abs((taken - f.taken).total_seconds()) <= 5:
                f.used = True
                batch.media(eid, f.path, "alt", original_filename=fname)
                stats["file_alt"] += 1

    # --- üçüncü geçiş: hiçbir asset'e eşleşmeyen (XMP zamanlı) dosyalar → XMP'den asgari olay.
    # Zaman kesimi YOK: geleceğe tarihli tek asset kesimi kaydırırdı; snapshot sonrası eklenen eski
    # tarihli dosyalar da görünür olmalı.
    rank = {"original": 0, "adjusted": 1, "live": 2}
    for stem, files in disk.items():
        fresh = sorted((f for f in files if not f.used and f.taken is not None),
                       key=lambda f: (f.taken, rank.get(f.role, 3)))
        while fresh:
            head = min((f for f in fresh if abs((f.taken - fresh[0].taken).total_seconds()) <= 5),
                       key=lambda f: rank.get(f.role, 3))
            group = [f for f in fresh if abs((f.taken - head.taken).total_seconds()) <= 5]
            fresh = [f for f in fresh if f not in group]
            video = head.ext in VIDEO_EXT and head.role != "live"
            rel = head.path.relative_to(library) if head.path.is_relative_to(library) else head.path
            off = head.taken.utcoffset()
            eid = batch.event(
                f"file:{rel}", "video" if video else "photo", head.taken,
                tz_offset_min=int(off.total_seconds() // 60) if off is not None else None,
                title=head.path.name, lat=head.lat, lon=head.lon,
                payload={"from": "icloud-xmp", "snapshot": db_path.parent.name})
            batch.tag(eid, "icloud-only", "favorite" if any(f.favorite for f in group) else None,
                      "hidden" if any(f.hidden for f in group) else None)
            for f in group:
                f.used = True
                batch.media(eid, f.path, f.role, original_filename=f.path.name)
            stats["icloud_only"] += 1

    stats["events"] = batch.count
    stats["people"] = len({pk for s in faces.values() for pk in s})
    stats["disk_files"] = sum(len(v) for v in disk.values())
    stats["disk_unused"] = sum(1 for v in disk.values() for f in v if not f.used)
    return dict(stats)
