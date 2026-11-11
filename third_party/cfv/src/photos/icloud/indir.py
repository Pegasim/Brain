"""iCloud Fotoğraflar → icloud-sync: yalnız EKSİK dosyaları indirir (icloudpd CLI yerine). cfv-photos, 2026-11-10.

Neden icloudpd CLI değil:
  - Laptop arşivindeki ~11k asıl dosya, eski --set-exif-datetime yüzünden iCloud'dan birkaç bayt farklı.
    icloudpd (NAME_SIZE_DEDUP_WITH_SUFFIX) bunları "farklı" sayıp '<ad>-<boyut>.JPG' kopyası indiriyor — laptopta
    karantinaya alınan 11.842 kopyanın aynısı. Burada: o adda dosya varsa ve fark ≤4 KB (exif) ise indirilmez
    (farkı rapora yazılır). Fark >4 KB ise başka bir dosyadır (AirDrop, sayaç sarması) → icloudpd gibi
    '<ad>-<boyut>' yoluna indirilir.
  - icloudpd .part'ı boyut kontrolü yapmadan yerine koyuyor; burada boyut tutmadan yerine konmaz, fsync'siz konmaz.
  - icloudpd kendi .xmp'lerini her turda yeniden yazıyor; burada .xmp yalnız yoksa yazılır (O_EXCL: var olan
    .xmp'nin üzerine hiçbir durumda yazılmaz). İçerik icloudpd'nin build_metadata/generate_xml'i ile aynı.
Yol/ad hesabı icloudpd 1.32.3'ün kendi fonksiyonlarıyla (icloud_yol.py) → arşivle birebir aynı adlar.

Kurallar: hiçbir dosyayı silmez, üzerine yazmaz (yerine koyma os.link ile: hedef varsa hata).
Kullanım: indir.py gunluk|tam|tam-video [--liste]   (--liste: indirmez, eksikleri yazar)
          indir.py --oturum                         (yalnız oturum testi)
Ortam: ICLOUD_USER, ICLOUD_DIR, ICLOUD_COOKIES, ICLOUD_STATE, ICLOUD_UNTIL (gunluk: art arda tam öğe, 300)
Her yerleşen dosya (ve .xmp'si) $ST/indirilenler.txt'ye eklenir (ikinci-kopya.sh bunu okur).
Log satırları icloudpd biçiminde ('INFO     Downloaded <yol>'), kontrol.sh bunları sayar.
Çıkış: 0 tamam, 1 bazı dosyalar inmedi, 2 giriş gerekiyor (Tuna 2FA), 3 ağ/servis hatası, 4 kullanım/kilit.
"""

import datetime as dt
import fcntl
import os
import sys
import time
from dataclasses import replace
from xml.etree import ElementTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import icloud_yol as Y  # noqa: E402
from icloudpd.download import set_utime  # noqa: E402
from icloudpd.xmp_sidecar import build_metadata, generate_xml  # noqa: E402
from pyicloud_ipd.item_type import AssetItemType  # noqa: E402

UNTIL = int(os.environ.get("ICLOUD_UNTIL", "300"))
CHUNK = 1 << 20


class _Log:
    """icloudpd biçiminde stdout satırları."""
    def _w(self, level, msg, *a):
        print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {level:<8} {msg % a if a else msg}", flush=True)
    def info(self, m, *a): self._w("INFO", m, *a)
    def error(self, m, *a): self._w("ERROR", m, *a)


log = _Log()


def _fsync_dir(d: str) -> None:
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _note(path: str) -> None:
    """Yerleşen dosyayı ikinci kopya listesine ekle."""
    with open(os.path.join(Y.ST, "indirilenler.txt"), "a") as f:
        f.write(path + "\n")


def fetch(session, job: Y.Job, created: dt.datetime) -> bool:
    """job.path'e indirir. Boyut tutmazsa yerine KOYMAZ. Hedef varsa dokunmaz."""
    d = os.path.dirname(job.path)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "." + os.path.basename(job.path) + ".cfv-part")
    for attempt in range(6):
        have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        if have > job.size:  # bozuk birikim → geçici dosyayı yeniden başlat (asıl arşiv dosyası değil)
            os.truncate(tmp, 0)
            have = 0
        try:
            if have < job.size:  # tam boyuttaki .cfv-part (link'ten önce ölmüş tur) için istek yok (416 olurdu)
                r = session.get(job.version.url, headers={"Range": f"bytes={have}-"}, stream=True, timeout=(30, 300))
                if r.status_code in (403, 410):
                    log.error("Link süresi dolmuş (%s): %s — sonraki turda yeniden listelenecek", r.status_code, job.path)
                    return False
                if r.status_code not in (200, 206):
                    raise IOError(f"HTTP {r.status_code}")
                with open(tmp, "ab" if (have and r.status_code == 206) else "wb") as f:
                    for chunk in r.iter_content(CHUNK):
                        f.write(chunk)
                    f.flush()
                    os.fsync(f.fileno())
            got = os.path.getsize(tmp)
            if got != job.size:
                raise IOError(f"boyut {got} != {job.size}")
            os.link(tmp, job.path)  # hedef varsa FileExistsError → üzerine yazma yok
            os.unlink(tmp)
            _fsync_dir(d)
            set_utime(job.path, created)
            _note(job.path)
            log.info("Downloaded %s", job.path)
            return True
        except FileExistsError:
            log.error("Hedef bu arada oluşmuş, dokunulmadı: %s", job.path)
            return False
        except Exception as e:  # ağ kesintisi / IncompleteRead → Range ile devam
            log.info("Deneme %d başarısız (%s): %s", attempt + 1, str(e)[:120], job.path)
            time.sleep(min(60, 5 * (attempt + 1)))
    log.error("İnmedi (6 deneme): %s", job.path)
    return False


def write_xmp(media_path: str, asset_record: dict) -> None:
    """<medya>.xmp yoksa yazar; varsa (ya da bu arada oluştuysa) dokunmaz."""
    doc = ElementTree.tostring(generate_xml(build_metadata(asset_record)), encoding="utf-8", xml_declaration=True)
    try:
        with open(media_path + ".xmp", "xb") as f:
            f.write(doc)
            f.flush()
            os.fsync(f.fileno())
    except FileExistsError:
        return
    _note(media_path + ".xmp")


def plan(job: Y.Job):
    """→ (ne, job, durum, diskteki bayt). ne: tamam | rapor (exif farkı/yarım: dokunma) | indir (job.path hedef)."""
    st, real, have = Y.state(job)
    if st == "tamam":
        return "tamam", job, st, have
    if st == "farkli" and abs(have - job.size) > Y.EXIF_FARK:
        return "indir", replace(job, path=job.dedup_path), st, have  # başka dosya: icloudpd gibi '-<boyut>'
    if st in ("farkli", "yarim"):
        return "rapor", replace(job, path=real), st, have
    return "indir", job, st, have


def main() -> int:
    args = sys.argv[1:]
    mode = next((a for a in args if not a.startswith("--")), "gunluk")
    only_list, only_auth = "--liste" in args, "--oturum" in args
    user = os.environ.get("ICLOUD_USER")
    if not user:
        log.error("ICLOUD_USER yok")
        return 4
    if not only_auth and mode not in ("gunluk", "tam", "tam-video"):
        log.error("bilinmeyen kip: %s", mode)
        return 4
    os.makedirs(Y.ST, exist_ok=True)
    if not (only_list or only_auth):
        lock = open(os.path.join(Y.ST, ".indir-kilit"), "w")  # sarmalayıcının .kilit'inden ayrı
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.error("Başka bir indir.py çalışıyor, çıkılıyor")
            return 4
    try:
        api = Y.login(user, Y.COOK)
    except Exception as e:  # ağ kesintisi, Apple 5xx: giriş sorunu değil
        log.error("Ağ/servis hatası (%s: %s)", type(e).__name__, str(e)[:160])
        return 3
    if api is None:
        log.error("Authentication required: oturum geçersiz, giris.sh ile yeniden giriş gerekli")
        return 2
    if only_auth:
        log.info("Oturum geçerli")
        return 0
    log.info("Oturum geçerli, kip=%s%s", mode, " (yalnız liste)" if only_list else "")
    farkli_f = open(os.path.join(Y.ST, f"farkli-{dt.date.today():%Y-%m-%d}.tsv"), "a")
    streak = n_items = n_dl = n_fail = n_diff = n_dedup = 0
    for photo in Y.library(api).all:
        if mode == "tam-video" and photo.item_type != AssetItemType.MOVIE:
            continue
        n_items += 1
        complete = True
        for job in Y.jobs(photo, Y.ROOT):
            what, job, st, have = plan(job)
            if what == "tamam":
                continue
            if what == "rapor":
                farkli_f.write(f"{st}\t{job.role}\t{job.path}\t{job.size}\t{have}\n")
                n_diff += 1
                continue
            complete = False
            n_dedup += st == "farkli"
            if only_list:
                print(job.path, flush=True)
                continue
            if fetch(api.photos.session, job, photo.created):
                n_dl += 1
                write_xmp(job.path, photo._asset_record)
            else:
                n_fail += 1
        streak = streak + 1 if complete else 0
        if mode == "gunluk" and streak >= UNTIL:
            log.info("Found %d consecutive previously downloaded photos. Exiting", UNTIL)
            break
    farkli_f.close()
    log.info("Bitti: öğe=%d indirilen=%d inmeyen=%d ayni_ad_baska_dosya=%d ayni_ad_exif_ya_da_yarim=%d",
             n_items, n_dl, n_fail, n_dedup, n_diff)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
