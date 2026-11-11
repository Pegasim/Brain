# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy", "scikit-learn", "sqlite-vec"]
# ///
"""Ekran görüntüsü → kaynak uygulama/tür tahmini (OCR + boyut kuralları, kalanı CLIP vektörü + küçük sınıflandırıcı).

    uv run src/photos/apps.py tahmin [etiket.tsv]   tüm ekran görüntüleri → apps.tsv
    uv run src/photos/apps.py olc etiket.tsv        etiketli örnekte doğruluk (sınıflandırıcı için birini-dışarıda-bırak)

etiket.tsv: <asset uuid>\\t<sınıf> (gözle etiketlenmiş pilot; kişisel veri, data/ altında tutulur).
Sınıflar: SINIFLAR. "gorsel" = aslında ekran görüntüsü değil (ekranın/kâğıdın fotoğrafı, afiş): iPhone
WhatsApp'tan kaydedilenlerin bir kısmını da ekran görüntüsü işaretliyor.
"""
import os, re, sqlite3, sys
from collections import Counter
from pathlib import Path

import numpy as np

DATA = Path(os.environ.get("CFV_DATA", "data"))
OUT = DATA / "processed" / "photos"
SINIFLAR = ["instagram", "whatsapp", "snapchat", "sohbet_diger", "sosyal_diger", "video", "muzik", "tarayici",
            "harita", "eposta", "banka_alisveris", "belge_kod", "ai_sohbet", "sistem", "fotograflar", "oyun",
            "goruntulu_arama", "gorsel", "diger"]

# (sınıf, OCR metninde aranan desen) — sıra önemli: ilk tutan kazanır
KURAL = [
    ("snapchat", r"send a chat|sohbet gönder|snapchat"),
    ("instagram", r"instagram|liked by|beğenenler|yorum ekle|add a comment|sticker taps|gönderi\b|your story|hikayen"),
    ("ai_sohbet", r"chatgpt|reply to claude|claude can make mistakes|how can i help you|ask anything|message chatgpt|gemini"),
    ("whatsapp", r"whatsapp|grup bilgisi|group info|search members|okunmamış mesaj|unread messages?|bir mesaj yazın|"
                 r"last seen|son görülme|çevrimiçi\b|\bonline\b.*\d\d:\d\d"),
    ("video", r"youtube|abone ol|subscribe|\d+[.,]?\d*\s?[kmb]? views|görüntüleme|shorts|netflix|diziler \| filmler"),
    ("muzik", r"spotify|apple music|music\.apple|\blibrary\b.*\bradio\b|replay'?\d\d|\bshuffle\b"),
    ("harita", r"directions|yol tarifi|suggested routes|önerilen rotalar|depart now|moovit|\bmaps\b|hybrid \| satellite"),
    ("sosyal_diger", r"x\.com|reddit|\br/\w+|linkedin|followers|takipçi|repost|retweet|upvotes"),
    ("eposta", r"gmail|inbox|gelen kutusu|\bkime\b|tümünü yanıtla|reply all|e-posta"),
    ("belge_kod", r"c:\\|ps c:|terminal|commits?\b|authored|#include|\bdef |import |\.py\b|\.cpp\b|solidworks|onshape|"
                  r"explorer|debug console|\bR6\d\d\b"),
    ("banka_alisveris", r"\biban\b|sepete ekle|add to cart|sipariş|kargo|\d[\d.,]*\s?(tl|₺)\b|ödeme|payment|kart no|"
                        r"hesap hareket|kart hareket|faiz|para transferi|koltuk seç|bilet|\bpnr\b|prime\b"),
    ("sistem", r"slide to power off|medical id|swipe up to unlock|settings|ayarlar|recents\b.*keypad|"
               r"emergency call|focus|notification|bildirim"),
    ("fotograflar", r"shared albums|paylaşılan albümler|recents \| \d|\bfor you\b"),
    ("tarayici", r"google|https?://|www\.|\.com\b|\.org\b|\.edu|\.tr\b|\.net\b|\.ai\b"),
]
C = float(os.environ.get("CFV_APPS_C", 2))  # sınıflandırıcı düzenlileştirme
TELEFON = 0.40, 0.60  # telefon ekranı en/boy (9:19.5 … 9:16); kamera fotoğrafı 3:4 / 9:16 büyük boyut


def boyut(w, h):
    """en/boy ve kamera fotoğrafı ipucu"""
    r = min(w, h) / max(w, h)
    kamera = max(w, h) in (2048, 3724, 3840, 4032) and (abs(r - 0.75) < 0.02 or abs(r - 0.5625) < 0.01) \
        and min(w, h) not in (750, 1080, 1170)
    return r, kamera


def kural(ocr, bundle, w, h):
    t = (ocr or "").lower()
    if bundle and "picaboo" in bundle:
        return "snapchat"
    _, kamera = boyut(w, h)
    if kamera:
        return "gorsel"
    for s, p in KURAL:
        if re.search(p, t):
            return s
    return None


def veri():
    """[(uuid, ocr, bundle, w, h)] — photos-meta is_screenshot=1 olan fotolar"""
    metas = sorted(DATA.glob("raw/iphone/photos-meta-*/photos-meta.sqlite"))
    m = sqlite3.connect(f"file:{metas[-1]}?mode=ro&immutable=1", uri=True)
    return m.execute("""SELECT a.uuid, o.text, coalesce(a.imported_by_bundle,''), a.width, a.height FROM asset a
        LEFT JOIN ocr o USING(uuid) WHERE a.is_screenshot = 1 AND a.kind = 'photo'""").fetchall()


def vektorler(keys):
    import sqlite_vec
    db = sqlite3.connect(OUT / "vectors.sqlite")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    out = {}
    for k in keys:
        r = db.execute("SELECT v.embedding FROM vec v JOIN item i ON i.id = v.rowid WHERE i.key = ?", (k,)).fetchone()
        if r:
            out[k] = np.frombuffer(r[0], dtype=np.float32)
    return out


def ozellik(v, w, h, bundle):
    r, kamera = boyut(w, h)
    return np.concatenate([v, [r, float(kamera), float(TELEFON[0] <= r <= TELEFON[1]), float("whatsapp" in bundle),
                               float("springboard" in bundle)]])


def etiketler(p):
    return dict(l.rstrip("\n").split("\t")[:2] for l in open(p) if "\t" in l)


def olc(p):
    from sklearn.linear_model import LogisticRegression
    lab = etiketler(p)
    rows = {r[0]: r for r in veri() if r[0] in lab}
    vs = vektorler(rows)
    keys = [k for k in lab if k in rows and k in vs]
    X = np.stack([ozellik(vs[k], rows[k][3], rows[k][4], rows[k][2]) for k in keys])
    y = np.array([lab[k] for k in keys])
    rule = [kural(rows[k][1], rows[k][2], rows[k][3], rows[k][4]) for k in keys]
    # sınıflandırıcı: birini dışarıda bırak
    clf_pred = []
    for i in range(len(keys)):
        tr = np.arange(len(keys)) != i
        c = LogisticRegression(C=C, max_iter=2000).fit(X[tr], y[tr])
        clf_pred.append(c.predict(X[i:i + 1])[0])
    comb = [r or c for r, c in zip(rule, clf_pred)]
    n = len(keys)
    hit = [r for r in rule if r]
    print(f"{n} etiketli ekran görüntüsü")
    print(f"kural kapsadı {len(hit)}/{n}, kuralın doğruluğu {sum(r == t for r, t in zip(rule, y) if r)}/{len(hit)}")
    print(f"yalnız sınıflandırıcı (LOO) {sum(c == t for c, t in zip(clf_pred, y))}/{n}")
    print(f"kural + sınıflandırıcı {sum(c == t for c, t in zip(comb, y))}/{n}")
    print("sınıf bazında (doğru/toplam):", {s: f"{sum(c == t == s for c, t in zip(comb, y))}/{sum(t == s for t in y)}"
                                           for s in SINIFLAR if s in y})
    print("en sık karışanlar:", Counter((t, c) for c, t in zip(comb, y) if c != t).most_common(8))


def tahmin(p=None):
    from sklearn.linear_model import LogisticRegression
    rows = veri()
    vs = vektorler([r[0] for r in rows])
    clf = None
    if p:
        lab = etiketler(p)
        tr = [r for r in rows if r[0] in lab and r[0] in vs]
        clf = LogisticRegression(C=C, max_iter=2000).fit(
            np.stack([ozellik(vs[r[0]], r[3], r[4], r[2]) for r in tr]), [lab[r[0]] for r in tr])
    dst = OUT / "apps.tsv"
    if dst.exists():
        dst.rename(OUT / f"apps.{int(dst.stat().st_mtime)}.tsv")
    c = Counter()
    with open(dst, "w") as f:
        print("kimlik\tuygulama\tkaynak", file=f)
        for k, ocr, b, w, h in rows:
            s, src = kural(ocr, b, w, h), "kural"
            if s is None and clf is not None and k in vs:
                s, src = clf.predict(ozellik(vs[k], w, h, b)[None])[0], "clip"
            s = s or "diger"
            c[s] += 1
            print(k, s, src, sep="\t", file=f)
    print(f"{sum(c.values())} ekran görüntüsü → {dst}")
    print(c.most_common())


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"olc": lambda: olc(sys.argv[2]), "tahmin": lambda: tahmin(*sys.argv[2:3])}.get(cmd, lambda: sys.exit(__doc__))()
