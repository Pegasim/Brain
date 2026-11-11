"""iCloud öğesi → icloud-sync içindeki beklenen yollar (icloudpd 1.32.3'ün KENDİ fonksiyonlarıyla).

Ayarlar laptop arşiviyle aynı: --folder-structure {:%Y/%m}, --size original --size adjusted,
canlı foto videosu '<ad>_HEVC.MOV', ad çakışması '<ad>-<iCloud boyutu>.<uzt>'.
indir.py ve dogrula.py bunu paylaşır. Yol varsayılanları burada (bash karşılığı: ortam.sh).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from tzlocal import get_localzone

from icloudpd.base import build_filename_cleaner, lp_filename_concatinator
from icloudpd.filename_policies import create_filename_builder
from icloudpd.paths import local_download_path
from pyicloud_ipd.asset_version import AssetVersion, calculate_version_filename
from pyicloud_ipd.base import PyiCloudService
from pyicloud_ipd.exceptions import PyiCloudFailedLoginException
from pyicloud_ipd.file_match import FileMatchPolicy
from pyicloud_ipd.raw_policy import RawTreatmentPolicy
from pyicloud_ipd.utils import disambiguate_filenames
from pyicloud_ipd.version_size import AssetVersionSize, LivePhotoVersionSize

SIZES = [AssetVersionSize.ORIGINAL, AssetVersionSize.ADJUSTED]
_BUILDER = create_filename_builder(FileMatchPolicy.NAME_SIZE_DEDUP_WITH_SUFFIX, build_filename_cleaner(False))
_TZ = get_localzone()
EXIF_FARK = 4096  # eski --set-exif-datetime dosyayı en çok birkaç KB değiştirdi; üstü başka dosya

ROOT = os.path.normpath(os.environ.get("ICLOUD_DIR", "/mnt/depo1/foto/icloud-sync"))
COOK = os.environ.get("ICLOUD_COOKIES", "/home/tunapro/.config/icloudpd")
ST = os.environ.get("ICLOUD_STATE", "/home/tunapro/.local/state/cfv-icloudpd")


@dataclass
class Job:
    role: str            # original | adjusted | live
    path: str            # icloudpd'nin ilk tercih ettiği yol
    size: int            # iCloud'daki bayt
    version: AssetVersion

    @property
    def dedup_path(self) -> str:
        """Aynı adda farklı boyutlu dosya varsa icloudpd'nin kullandığı '-<boyut>' yolu."""
        return f"-{self.size}.".join(self.path.rsplit(".", 1))


def login(user: str, cookie_dir: str) -> PyiCloudService | None:
    """Yalnız kayıtlı oturumla giriş; şifre denemez. Oturum geçersizse (giriş/2FA gerekli) None.
    Ağ/servis hataları None DEĞİL, istisna olarak yükselir (çağıran yeniden dener)."""
    try:
        api = PyiCloudService("com", user, lambda: None, None, cookie_directory=cookie_dir)
    except PyiCloudFailedLoginException:
        return None
    # çerez geçersizse şifre sağlayıcı boş döner, authenticate() dsInfo'suz çıkar
    if "dsInfo" not in (getattr(api, "data", None) or {}):
        return None
    if api.requires_2fa or api.requires_2sa:
        return None
    return api


def library(api: PyiCloudService):
    return api.photos.private_libraries.get("PrimarySync", api.photos)


def jobs(photo, root: str) -> list[Job]:
    d = os.path.normpath(os.path.join(root, f"{photo.created.astimezone(_TZ):%Y/%m}"))
    vers = photo.versions_with_raw_policy(RawTreatmentPolicy.AS_IS)
    versions, overrides = disambiguate_filenames(vers, SIZES, photo, lp_filename_concatinator)
    base = _BUILDER(photo)
    out = []
    for size in SIZES:
        if size not in versions:
            continue  # adjusted yoksa original zaten listede
        v = versions[size]
        fn = calculate_version_filename(base, v, size, lp_filename_concatinator, photo.item_type,
                                        overrides.get(size))
        out.append(Job(size.value, local_download_path(fn, d), v.size, v))
    lp = vers.get(LivePhotoVersionSize.ORIGINAL)
    if lp is not None:
        fn = calculate_version_filename(base, lp, LivePhotoVersionSize.ORIGINAL, lp_filename_concatinator,
                                        photo.item_type)
        out.append(Job("live", os.path.join(d, fn), lp.size, lp))
    return out


def state(job: Job) -> tuple[str, str, int | None]:
    """Diskteki durum: tamam | farkli (aynı ad, başka boyut, -<boyut> yok) | yarim (-<boyut> var ama eksik) | eksik."""
    if os.path.isfile(job.path):
        have = os.path.getsize(job.path)
        if have == job.size:
            return "tamam", job.path, have
        alt = job.dedup_path
        if os.path.isfile(alt):
            have2 = os.path.getsize(alt)
            return ("tamam" if have2 == job.size else "yarim"), alt, have2
        return "farkli", job.path, have
    return "eksik", job.path, None
