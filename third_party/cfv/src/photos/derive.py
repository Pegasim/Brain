# /// script
# requires-python = ">=3.12"
# dependencies = ["sentence-transformers>=6.2", "torch==2.15.0", "sqlite-vec", "pillow", "pillow-heif>=1.8", "opencv-python-headless", "numpy"]
# ///
"""Ekran görüntüsü / türev → kaynak foto eşleme (CLIP aday + SIFT/RANSAC doğrulama).
Toplu iş: `nice -n 19 ionice -c3 uv run ...` (en çok CFV_WORKERS=4 işçi).

    uv run src/photos/derive.py mid             galeri fotoları → mid/<uuid>.jpg (768 px, bir kez)
    uv run src/photos/derive.py match           ekran görüntüleri + WhatsApp kayıtları/PNG/düzenlenmiş → derivs.tsv
                                                (tur: ekran / kayit / png / duzenleme; signals.go kopya kümesine katar)
    uv run src/photos/derive.py synth [n]       sentetik ekran görüntüsüyle geri çağırma ölçümü
"""
import os, random, sqlite3, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import Pool
from pathlib import Path

import cv2
import numpy as np

cv2.setNumThreads(1)  # Pool fork'u: üst süreçte cv2 iş parçacığı havuzu açılırsa çocuklar kilitleniyor

DATA = Path(os.environ.get("CFV_DATA", "data"))
OUT = DATA / "processed" / "photos"
MID = OUT / "mid"
WORKERS = int(os.environ.get("CFV_WORKERS", 4))  # paylaşımlı sunucu: en çok 4 (nice -n 19 ionice -c3 ile koş)
TOPK = int(os.environ.get("CFV_TOPK", 30))  # CLIP adayı (kırpım başına PER_CROP, birleşip en çok TOPK)
PER_CROP = int(os.environ.get("CFV_PER_CROP", 10))
MIN_INL = 15   # RANSAC inlier eşiği (synth ile ayarlanır)
MIN_COV = 0.6  # piksel kaplama eşiği (coverage; synth ile ayarlandı)


def ok(x):
    """aday (anahtar, cos, inlier, ölçek, kaplama) kabul mü"""
    return x[2] >= MIN_INL and x[4] >= MIN_COV


def best(cands):
    """kabul edilenler önde, sonra inlier'a göre"""
    return sorted(cands, key=lambda x: (not ok(x), -x[2]))


def midp(k):
    """mid/ dosyası; UUID'siz medyanın anahtarı 'file:<yol>' → dosya adında : ve / olmaz."""
    return MID / (k.replace(":", "-").replace("/", "_") + ".jpg")


def gallery():
    """[(uuid, yol, utc, ekran_görüntüsü_mü)] — dosyası diskte olan galeri fotoları.
    UUID'siz (yalnız iCloud'da olan) PNG'nin bayrağı yok: ekran görüntüsü sayılır, yoksa aynı arayüzü kaynak sanıyor."""
    db = sqlite3.connect(f"file:{DATA}/processed/cfv.sqlite?mode=ro", uri=True)
    metas = sorted(DATA.glob("raw/iphone/photos-meta-*/photos-meta.sqlite"))
    db.execute(f"ATTACH 'file:{metas[-1]}?mode=ro&immutable=1' AS m")
    rows = db.execute("""SELECT e.external_id, (SELECT m.path FROM media m WHERE m.event_id = e.id
        AND m.role IN ('adjusted','original','alt') ORDER BY m.role = 'adjusted' DESC, m.role = 'original' DESC LIMIT 1),
        e.start_utc, coalesce(a.is_screenshot, upper(e.external_id) LIKE 'FILE:%.PNG') FROM event e LEFT JOIN m.asset a ON a.uuid = e.external_id
        WHERE e.kind = 'photo'""").fetchall()
    return [r for r in rows if r[1] and os.path.exists(r[1])]


def mid():
    """mid/<anahtar>.jpg; mid/yollar.tsv hangi dosyadan üretildiğini tutar, yolu değişen yeniden üretilir
    (eskisi mid-eski/'ye taşınır, silinmez)."""
    MID.mkdir(exist_ok=True)
    log = MID / "yollar.tsv"
    made = dict(l.rstrip("\n").split("\t") for l in open(log)) if log.exists() else {}
    todo = []
    for k, p, _, _ in gallery():
        if not midp(k).exists():
            todo.append((k, p))
        elif made.get(k, p) != p:
            (OUT / "mid-eski").mkdir(exist_ok=True)
            midp(k).rename(OUT / "mid-eski" / f"{midp(k).stem}.{int(midp(k).stat().st_mtime)}.jpg")
            todo.append((k, p))
    print(f"{len(todo)} yeni ya da yolu değişmiş", file=sys.stderr)

    def one(kp):
        k, p = kp
        tmp = midp(k).with_suffix(".tmp.jpg")  # yarıda kesilirse bozuk dosya "bitti" sayılmasın
        r = subprocess.run(["vips", "thumbnail", p, f"{tmp}[Q=80,strip]", "768", "--size", "down"],
                           capture_output=True, env=os.environ | {"VIPS_CONCURRENCY": "1"})
        if r.returncode == 0:
            tmp.rename(midp(k))
            return True
        try:  # vips'in açamadığı HEIC (derinlik haritalı Portre; sistem libheif 1.17 eski) → pillow-heif
            import pillow_heif
            from PIL import Image
            pillow_heif.register_heif_opener()
            im = Image.open(p)
            im.thumbnail((768, 768))
            im.convert("RGB").save(tmp, "JPEG", quality=80)
            tmp.rename(midp(k))
            return True
        except Exception as e:
            print(f"{p}: {e}", file=sys.stderr)
            return False

    ok = 0
    with ThreadPoolExecutor(WORKERS) as ex, open(log, "a") as f:
        for i, (r, (k, p)) in enumerate(zip(ex.map(one, todo), todo)):
            ok += r
            if r:
                print(k, p, sep="\t", file=f, flush=True)
            if i % 2000 == 0:
                print(f"{i}/{len(todo)}", file=sys.stderr)
    print(f"mid: {ok}/{len(todo)} üretildi")


def crops(img):
    """Ekran görüntüsünden aday bölgeler: tam, orta, üst/orta/alt yarı, arayüz şeritleri kırpılmış kutu."""
    h, w = img.shape[:2]
    out = [img, img[h // 6: h * 5 // 6], img[: h // 2], img[h // 4: h * 3 // 4], img[h // 2:]]
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    rows = np.where(cv2.Sobel(g, cv2.CV_32F, 0, 1).__abs__().mean(1) > 6)[0]  # düz arayüz şeritleri dışı
    if len(rows) > h // 5:
        out.append(img[rows[0]: rows[-1] + 1])
    return out


def clip_model():
    import torch
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer("clip-ViT-B-32", device="cuda" if torch.cuda.is_available() else "cpu")


def gallery_vectors(keys):
    import sqlite_vec
    db = sqlite3.connect(OUT / "vectors.sqlite")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    want, ks, vs = set(keys), [], []
    for k, e in db.execute("SELECT i.key, v.embedding FROM item i JOIN vec v ON v.rowid = i.id WHERE i.kind = 'photo'"):
        if k in want:
            ks.append(k)
            vs.append(np.frombuffer(e, dtype=np.float32))
    return ks, np.stack(vs)


def candidates(model, imgs, keys, mat):
    """her görüntü → [(aday, en iyi cos)] (kırpımların top-5'leri birleşir, en çok TOPK)."""
    from PIL import Image
    res = []
    for i in range(0, len(imgs), 64):
        cs = [crops(load(im)) for im in imgs[i:i + 64]]
        flat = [Image.fromarray(cv2.cvtColor(c, cv2.COLOR_BGR2RGB)) for cc in cs for c in cc]
        e = model.encode(flat, batch_size=128, normalize_embeddings=True)
        sims, j = e @ mat.T, 0
        for cc in cs:
            s = sims[j: j + len(cc)]
            j += len(cc)
            best = {}
            for row in s:
                for t in np.argpartition(-row, PER_CROP)[:PER_CROP]:
                    best[t] = max(best.get(t, 0), row[t])
            top = sorted(best.items(), key=lambda x: -x[1])[:TOPK]
            res.append([(keys[t], float(v)) for t, v in top])
    return res


def feats(img, n):
    g = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    kp, d = cv2.SIFT_create(nfeatures=n).detectAndCompute(g, None)
    return np.float32([k.pt for k in kp]).reshape(-1, 2), d


def verify(a, b):
    """a: türev (ekran görüntüsü), b: kaynak; → (inlier, ölçek, M). Kaynaktan türeve benzerlik dönüşümü."""
    (pa, da), (pb, db) = a, b
    if da is None or db is None or len(da) < 8 or len(db) < 8:
        return 0, 0.0, None
    m = cv2.BFMatcher(cv2.NORM_L2).knnMatch(db, da, k=2)
    good = [x[0] for x in m if len(x) == 2 and x[0].distance < 0.75 * x[1].distance]
    if len(good) < 8:
        return 0, 0.0, None
    src = pb[[g.queryIdx for g in good]]
    dst = pa[[g.trainIdx for g in good]]
    M, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=5)
    if M is None:
        return 0, 0.0, None
    scale = float(np.hypot(M[0, 0], M[1, 0]))
    if not 0.05 < scale < 4:
        return 0, scale, None
    # tek noktaya yığılmış eşleşmeler (logo, yazı) sayılmasın: inlier'lar kaynakta yayılmış olmalı
    p = src[inl.ravel() == 1]
    if len(p) < 4 or np.ptp(p[:, 0]) * np.ptp(p[:, 1]) < 0.02 * np.ptp(pb[:, 0]) * np.ptp(pb[:, 1]):
        return 0, scale, None
    return int(inl.sum()), scale, M


def coverage(ga, gb, M, B=16):
    """kaynağı (gb) M ile türevin (ga) üstüne koy; örtüşen 16 px blokların kaçında piksel korelasyonu ≥0.6.
    Aynı foto ≈1; aynı ürün/sahne/şablonun başka fotosu yalnız bir bölgede tutar."""
    h, w = ga.shape
    wb = cv2.warpAffine(cv2.GaussianBlur(gb, (3, 3), 0), M, (w, h)).astype(np.float32)
    mask = cv2.warpAffine(np.full_like(gb, 255), M, (w, h), flags=cv2.INTER_NEAREST) == 255
    ga = cv2.GaussianBlur(ga, (3, 3), 0).astype(np.float32)
    h, w = h // B * B, w // B * B
    blk = lambda x: x[:h, :w].reshape(h // B, B, w // B, B).transpose(0, 2, 1, 3).reshape(-1, B * B)
    x, y, m = blk(ga), blk(wb), blk(mask).all(1)
    x, y = x[m] - x[m].mean(1, keepdims=True), y[m] - y[m].mean(1, keepdims=True)
    sx, sy = np.sqrt((x * x).mean(1)), np.sqrt((y * y).mean(1))
    busy = (sx > 4) & (sy > 4)  # yalnız iki tarafı da desenli bloklar (düz arayüz alanı sayılmaz)
    if busy.sum() < 8:
        return 0.0
    ncc = (x * y).mean(1)[busy] / (sx[busy] * sy[busy] + 1e-6)
    return float(np.mean(ncc >= 0.6))


def load(x):
    return x if isinstance(x, np.ndarray) else cv2.imread(str(x))


_F = {}  # kaynak öznitelikleri (fork ile işçilere paylaşılır); tanımlayıcılar uint8 (SIFT değerleri 0-255 tamsayı)


def _src_feat(k):
    im = cv2.imread(str(midp(k)), cv2.IMREAD_GRAYSCALE)
    if im is None:
        return k, (np.zeros((0, 2), np.float32), None)
    pts, d = feats(im, 1000)
    return k, (pts, None if d is None else d.astype(np.uint8))


def _match(job):
    """→ [(aday, cos, inlier, ölçek, kaplama)]; kaplama yalnız inlier ≥ MIN_INL olanlarda hesaplanır."""
    img, cands = job
    ga = cv2.cvtColor(load(img), cv2.COLOR_BGR2GRAY)
    a = feats(ga, 2500)
    out = []
    for k, c in cands:
        if k in _F:
            pts, d = _F[k]
            inl, sc, M = verify(a, (pts, None if d is None else d.astype(np.float32)))
            cov = coverage(ga, cv2.imread(str(midp(k)), cv2.IMREAD_GRAYSCALE), M) if inl >= MIN_INL else 0.0
            out.append((k, c, inl, sc, cov))
    return out


def run(imgs, keys, mat, model):
    """görüntüler (dizi ya da dosya yolu) → her biri için [(aday, cos, inlier, ölçek)]."""
    cs = candidates(model, imgs, keys, mat)
    need = sorted({k for c in cs for k, _ in c} - _F.keys())
    with Pool(WORKERS) as p:
        _F.update(p.map(_src_feat, need, chunksize=32))
    with Pool(WORKERS) as p:
        return p.map(_match, list(zip(imgs, cs)), chunksize=4)


def turler():
    """ekran görüntüsü olmayan türev adayları: uuid → tür (WhatsApp'tan kaydedilmiş / PNG / düzenlenmiş)."""
    metas = sorted(DATA.glob("raw/iphone/photos-meta-*/photos-meta.sqlite"))
    m = sqlite3.connect(f"file:{metas[-1]}?mode=ro&immutable=1", uri=True)
    return dict(m.execute("""SELECT uuid, CASE WHEN imported_by_bundle LIKE '%whatsapp%' THEN 'kayit'
        WHEN upper(original_filename) LIKE '%.PNG' THEN 'png' ELSE 'duzenleme' END FROM asset
        WHERE kind = 'photo' AND coalesce(is_screenshot, 0) = 0 AND (imported_by_bundle LIKE '%whatsapp%'
        OR upper(original_filename) LIKE '%.PNG' OR editor_bundle IS NOT NULL)"""))


def match():
    """ekran görüntüleri (photos-meta is_screenshot) + kaydedilmiş/PNG/düzenlenmiş fotolar → derivs.tsv:
    en çok inlier alan kaynak (≥ MIN_INL). Ekran görüntüsü olmayan türevin kaynağı kendisi olamaz ve ondan
    önce çekilmiş olmalı (ilişki tek yönlü kalsın)."""
    rows = [r for r in gallery() if (midp(r[0])).exists()]
    tur = turler()
    shots = [r for r in rows if r[3]] + [r for r in rows if not r[3] and r[0] in tur]
    when = {r[0]: r[2] or "" for r in rows}
    keys, mat = gallery_vectors([r[0] for r in rows if not r[3]])
    day = {r[0]: r[2][:10] for r in rows}
    model = clip_model()
    dst = OUT / "derivs.tsv"
    if dst.exists():  # eski çıktı yedeklenir
        dst.rename(OUT / f"derivs.{int(dst.stat().st_mtime)}.tsv")
    n = 0
    with open(dst, "w") as f:
        print("turev\tkaynak\ttur\tinlier\tkaplama\tikinci\tcos\tolcek\tturev_gun\tkaynak_gun", file=f)
        for i in range(0, len(shots), 500):
            chunk = shots[i:i + 500]
            for r, cands in zip(chunk, run([midp(r[0]) for r in chunk], keys, mat, model)):
                t = "ekran" if r[3] else tur[r[0]]
                if not r[3]:
                    cands = [c for c in cands if c[0] != r[0] and when[c[0]] < when[r[0]]]
                cands = best(cands)
                if cands and ok(cands[0]):
                    k, cos, inl, sc, cov = cands[0]
                    second = cands[1][2] if len(cands) > 1 else 0
                    print(r[0], k, t, inl, f"{cov:.2f}", second, f"{cos:.3f}", f"{sc:.2f}", day[r[0]], day[k],
                          sep="\t", file=f)
                    n += 1
            f.flush()
            print(f"{i + len(chunk)}/{len(shots)} türev adayı, {n} eşleşme", file=sys.stderr)
    print(f"{len(shots)} türev adayı, {n} tanesi bir kaynağa eşleşti → {dst}")


def fake_screenshot(img, rnd):
    """kaynağı telefon arayüzüne göm: rastgele kırp, ekran genişliğine ölçekle, üst/alt çubuk + yazı, JPEG."""
    h, w = img.shape[:2]
    x0, x1 = int(w * rnd.uniform(0, .2)), int(w * (1 - rnd.uniform(0, .2)))
    y0, y1 = int(h * rnd.uniform(0, .2)), int(h * (1 - rnd.uniform(0, .2)))
    img = img[y0:y1, x0:x1]
    W, H = 355, 768
    bg = rnd.choice([255, 0, 30, 245])
    canvas = np.full((H, W, 3), bg, np.uint8)
    fw = int(W * rnd.uniform(.6, 1))
    fh = min(int(img.shape[0] * fw / img.shape[1]), H - 200)
    fw = int(img.shape[1] * fh / img.shape[0])
    top = rnd.randint(60, H - fh - 100)
    left = (W - fw) // 2
    canvas[top:top + fh, left:left + fw] = cv2.resize(img, (fw, fh), interpolation=cv2.INTER_AREA)
    ink = (255 - bg,) * 3
    for y in [18, 45, top + fh + 25, top + fh + 50, H - 30]:
        cv2.putText(canvas, "".join(rnd.choice("abcdefghijk 0123:") for _ in range(18)), (10, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, ink, 1)
    cv2.circle(canvas, (25, top - 22), 14, ink, -1)
    ok, buf = cv2.imencode(".jpg", canvas, [cv2.IMWRITE_JPEG_QUALITY, rnd.randint(55, 90)])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def groups():
    """dHash kopya grupları (signals.go ile aynı ölçüt, ≤24): yol → grup kimliği; synth'te kopya da doğru sayılır."""
    hs = {}
    for line in open(OUT / "hash256.tsv"):
        p, h = line.rstrip("\n").split("\t")
        hs[p] = int(h, 16)
    return hs


def synth(n=200):
    """kaynak + aynı an (dHash kopyası ya da ±10 dk seri çekim) doğru sayılır; başka andan gelen aday yanlış."""
    from datetime import datetime
    n = int(n)
    rnd = random.Random(7)
    src = [r for r in gallery() if not r[3] and (midp(r[0])).exists()]
    keys, mat = gallery_vectors([r[0] for r in src])
    path = {r[0]: r[1] for r in src}
    when = {r[0]: datetime.fromisoformat(r[2]).timestamp() for r in src}
    hs = groups()
    pick = rnd.sample(keys, n)
    imgs = [fake_screenshot(cv2.imread(str(midp(k))), rnd) for k in pick]
    res = run(imgs, keys, mat, clip_model())
    copy = lambda a, b: a == b or (path[a] in hs and path[b] in hs and bin(hs[path[a]] ^ hs[path[b]]).count("1") <= 24)
    moment = lambda a, b: copy(a, b) or abs(when[a] - when[b]) < 600
    in_cand = exact = burst = 0
    for k, r in zip(pick, res):
        in_cand += any(copy(c, k) for c, *_ in r)
        r = best(r)
        if r and ok(r[0]) and moment(r[0][0], k):
            exact += copy(r[0][0], k)
            burst += not copy(r[0][0], k)
    with open(OUT / "synth.tsv", "w") as f:  # inceleme için: sahte görüntü kaynağı, aday, cos, inlier, ölçek, kaplama, aynı an
        for k, r in zip(pick, res):
            for c, cos, inl, sc, cov in r:
                print(k, c, f"{cos:.3f}", inl, f"{sc:.2f}", f"{cov:.2f}", int(moment(c, k)), sep="\t", file=f)
    print(f"sentetik {n}: kaynak aday listesinde {in_cand}; kabul edilen en iyi: kaynak/kopya {exact}, aynı an seri {burst}")
    print(f"  inlier ≥{MIN_INL} iken kaplama eşiğine göre: doğru andan en iyi / başka andan kabul edilen aday")
    for t in (0, .2, .3, .4, .5, .6, .7, .8):
        hit = sum(any(moment(x[0], k) and x[2] >= MIN_INL and x[4] >= t for x in r) for k, r in zip(pick, res))
        fp = sum(1 for k, r in zip(pick, res) for x in r if not moment(x[0], k) and x[2] >= MIN_INL and x[4] >= t)
        print(f"  kaplama ≥{t:.1f}: geri çağırma {hit / n:.3f}, başka andan kabul {fp}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"mid": mid, "match": match, "synth": lambda: synth(*sys.argv[2:])}.get(cmd, lambda: sys.exit(__doc__))()
