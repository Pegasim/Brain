# /// script
# requires-python = ">=3.12"
# dependencies = ["sentence-transformers>=6.2", "torch==2.15.0", "sqlite-vec", "pillow", "pillow-heif>=1.8"]
# ///
"""Foto vektör veritabanı (CLIP, işlemcide) + sticker → kaynak foto eşleme.

    uv run src/photos/embed.py embed            galeri fotoları + WhatsApp stickerları → vectors.sqlite
    uv run src/photos/embed.py stickers         stickerlar: kaç kez gönderildi/geldi + en yakın foto → stickers.json
    uv run src/photos/embed.py search "metin"   Türkçe metinle foto ara (çok dilli CLIP)
"""
import hashlib, json, os, sqlite3, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import sqlite_vec
import torch
from PIL import Image
from sentence_transformers import SentenceTransformer

DATA = Path(os.environ.get("CFV_DATA", "data"))
OUT = DATA / "processed" / "photos"
SMALL = OUT / "small"
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
MATCH = 0.88  # sticker ↔ foto aday eşiği; ≥0.95 neredeyse hep doğru, altını Haiku doğrular (kaynak_foto)


def cfv():
    return sqlite3.connect(f"file:{DATA}/processed/cfv.sqlite?mode=ro", uri=True)


def vecdb():
    db = sqlite3.connect(OUT / "vectors.sqlite")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.execute("CREATE TABLE IF NOT EXISTS item (id INTEGER PRIMARY KEY, key TEXT UNIQUE, kind TEXT, path TEXT)")
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vec USING vec0(embedding float[512] distance_metric=cosine)")
    return db


def items():
    """(anahtar, tür, dosya): galeri fotoları (asset UUID) + farklı stickerlar (içerik md5)."""
    db = cfv()
    out = db.execute("""SELECT e.external_id, 'photo', (SELECT m.path FROM media m WHERE m.event_id = e.id
        AND m.role IN ('adjusted','original','alt') ORDER BY m.role = 'adjusted' DESC, m.role = 'original' DESC LIMIT 1)
        FROM event e WHERE e.kind = 'photo'""").fetchall()
    seen = set()
    for (p,) in db.execute("""SELECT m.path FROM event e JOIN media m ON m.event_id = e.id
            WHERE m.role = 'attachment' AND json_extract(e.payload,'$.type') = 'sticker'"""):
        if os.path.exists(p) and (k := "sticker-" + md5(p)) not in seen:
            seen.add(k)
            out.append((k, "sticker", p))
    return [(k, t, p) for k, t, p in out if p and os.path.exists(p)]


def md5(p):
    return hashlib.md5(Path(p).read_bytes()).hexdigest()[:16]


def fname(k):
    """UUID'siz medyanın anahtarı 'file:<yol>' → dosya adında : ve / olmaz."""
    return k.replace(":", "-").replace("/", "_")


def small(k, p):
    """224 px JPEG (şeffaf sticker beyaza basılır)."""
    dst = SMALL / f"{fname(k)}.jpg"
    if not dst.exists():
        r = subprocess.run(["vips", "thumbnail", p, f"{dst}[Q=85,background=255]", "224", "--height", "224"],
                           capture_output=True)
        if r.returncode and not pil_thumb(p, dst, (224, 224), quality=85):
            return None
    return dst


def pil_thumb(p, dst, box, **kw):
    """vips'in açamadığı HEIC (derinlik haritalı Portre; sistem libheif 1.17 eski) → pillow-heif ile küçült."""
    import pillow_heif
    pillow_heif.register_heif_opener()
    try:
        im = Image.open(p)
        im.thumbnail(box)
        tmp = dst.with_suffix(".tmp.jpg")
        im.convert("RGB").save(tmp, "JPEG", **kw)
        tmp.rename(dst)
        return True
    except Exception as e:
        print(f"{p}: {e}", file=sys.stderr)
        return False


def embed():
    SMALL.mkdir(parents=True, exist_ok=True)
    db = vecdb()
    have = dict(db.execute("SELECT key, path FROM item"))
    todo = [it for it in items() if it[0] not in have or it[1] == "photo" and have[it[0]] != it[2]]
    moved = [it for it in todo if it[0] in have]  # dosyası değişmiş: eski vektör silinir, eski küçük resim kenara
    for k, _, _ in moved:
        db.execute("DELETE FROM vec WHERE rowid = (SELECT id FROM item WHERE key = ?)", (k,))
        db.execute("DELETE FROM item WHERE key = ?", (k,))
        if (s := SMALL / f"{fname(k)}.jpg").exists():
            (OUT / "small-eski").mkdir(exist_ok=True)
            s.rename(OUT / "small-eski" / f"{fname(k)}.{int(s.stat().st_mtime)}.jpg")
    db.commit()
    print(f"{len(have)} vektör var, {len(todo) - len(moved)} yeni, {len(moved)} dosyası değişmiş", file=sys.stderr)
    with ThreadPoolExecutor(4) as ex:
        smalls = list(ex.map(lambda it: small(it[0], it[2]), todo))
    model = SentenceTransformer("clip-ViT-B-32", device=DEVICE)
    todo = [(it, s) for it, s in zip(todo, smalls) if s]
    for i in range(0, len(todo), 256):
        chunk = todo[i:i + 256]
        embs = model.encode([Image.open(s).convert("RGB") for _, s in chunk], batch_size=64, normalize_embeddings=True)
        for ((k, t, p), _), e in zip(chunk, embs):
            rid = db.execute("INSERT INTO item (key, kind, path) VALUES (?,?,?)", (k, t, p)).lastrowid
            db.execute("INSERT INTO vec (rowid, embedding) VALUES (?, ?)", (rid, e.astype("float32").tobytes()))
        db.commit()
        print(f"{i + len(chunk)}/{len(todo)}", file=sys.stderr)


def stickers():
    """Her farklı sticker: kaç kez gönderdin/geldi, sohbetler, ilk-son tarih, en yakın galeri fotoğrafı."""
    db, vdb = cfv(), vecdb()
    agg = {}
    for p, utc, chat, mine in db.execute("""SELECT m.path, e.start_utc, json_extract(e.payload,'$.chat'),
            coalesce(json_extract(e.payload,'$.from_me'),0) FROM event e JOIN media m ON m.event_id = e.id
            WHERE m.role = 'attachment' AND json_extract(e.payload,'$.type') = 'sticker' ORDER BY e.start_utc"""):
        if not os.path.exists(p):
            continue
        a = agg.setdefault("sticker-" + md5(p), {"yol": p, "gonderdin": 0, "geldi": 0, "sohbetler": {}, "ilk": utc})
        a["gonderdin" if mine else "geldi"] += 1
        a["sohbetler"][chat] = a["sohbetler"].get(chat, 0) + 1
        a["son"] = utc
    for k, a in agg.items():
        row = vdb.execute("SELECT v.embedding FROM vec v JOIN item i ON i.id = v.rowid WHERE i.key = ?", (k,)).fetchone()
        if not row:
            continue
        for key, dist in vdb.execute("""SELECT i.key, v.distance FROM vec v JOIN item i ON i.id = v.rowid
                WHERE v.embedding MATCH ? AND k = 20 ORDER BY v.distance""", (row[0],)):
            if not key.startswith("sticker-"):
                if 1 - dist >= MATCH:
                    a["foto"], a["benzerlik"] = key, round(1 - dist, 3)
                break
    (OUT / "stickers.json").write_text(json.dumps(agg, ensure_ascii=False, indent=1))
    print(f"{len(agg)} sticker, {sum('foto' in a for a in agg.values())} tanesi bir fotoğrafla eşleşti")


def search(q, n=20):
    vdb = vecdb()
    e = SentenceTransformer("sentence-transformers/clip-ViT-B-32-multilingual-v1", device=DEVICE).encode(
        q, normalize_embeddings=True)
    for key, path, dist in vdb.execute("""SELECT i.key, i.path, v.distance FROM vec v JOIN item i ON i.id = v.rowid
            WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance""", (e.astype("float32").tobytes(), n)):
        print(f"{1 - dist:.3f}\t{key}\t{path}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"embed": embed, "stickers": stickers, "search": lambda: search(" ".join(sys.argv[2:]))}.get(
        cmd, lambda: sys.exit(__doc__))()
