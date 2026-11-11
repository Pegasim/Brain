"""iCloud listesi ↔ disk tam karşılaştırması (salt okur; hiçbir dosyaya yazmaz/indirmez).

Her iCloud öğesi için beklenen yolu icloud_yol.py ile (icloudpd 1.32.3'ün kendi fonksiyonları) hesaplar
ve diskteki boyutla karşılaştırır.
Durumlar: tamam | eksik (indir.py sonraki turda indirir)
          | farkli (aynı adda başka boyut; çoğu eski --set-exif-datetime farkı: birkaç yüz bayt)
          | yarim  ('-<boyut>' ekli dosya o boyutta değil → elle bak; ör. 'IMG_1234-5000.JPG' 4999 bayt)
Çıktı: <state>/dogrula-<tarih>.tsv (yalnız tamam olmayanlar, fark baytıyla) + stdout'a JSON özet.
Elle çalıştırılan denetim aracı (zamanlanmış değil; haftalık 'tam' turu aynı durumları farkli-<tarih>.tsv'ye
yazar, kontrol.sh 'yarim' satırlarında alarm verir).
Çalıştırma: sudo -u tunapro env $(cat /etc/cfv-icloudpd.env) /opt/icloudpd/lib-venv/bin/python -I dogrula.py
Çıkış: 0 sorun yok/yalnız eksik, 1 yarim ya da büyük farkli var, 2 giriş gerekiyor, 3 ağ/servis hatası.
"""

import datetime as dt
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import icloud_yol as Y  # noqa: E402

ROOT, ST, EXIF_FARK = Y.ROOT, Y.ST, Y.EXIF_FARK


def main() -> int:
    user = os.environ.get("ICLOUD_USER") or sys.exit("ICLOUD_USER yok")
    try:
        api = Y.login(user, Y.COOK)
    except Exception as e:
        print(json.dumps({"durum": "ag-hatasi", "hata": type(e).__name__}))
        return 3
    if api is None:
        print(json.dumps({"durum": "giris-gerekiyor"}))
        return 2
    stats: Counter = Counter()
    rows = []
    for photo in Y.library(api).all:
        stats["oge"] += 1
        for job in Y.jobs(photo, ROOT):
            st, real, have = Y.state(job)
            if st == "farkli":
                st = "farkli_exif" if abs(have - job.size) <= EXIF_FARK else "farkli_buyuk"
            stats[st] += 1
            if st != "tamam":
                diff = "" if have is None else have - job.size
                rows.append((st, job.role, real, job.size, "" if have is None else have, diff))
    os.makedirs(ST, exist_ok=True)
    out = os.path.join(ST, f"dogrula-{dt.date.today():%Y-%m-%d}.tsv")
    with open(out, "w") as f:
        f.write("durum\trol\tyol\ticloud_bayt\tdisk_bayt\tfark\n")
        for r in sorted(rows):
            f.write("\t".join(map(str, r)) + "\n")
    print(json.dumps({"durum": "bitti", "rapor": out, **stats}, ensure_ascii=False))
    return 1 if stats["yarim"] or stats["farkli_buyuk"] else 0


if __name__ == "__main__":
    sys.exit(main())
