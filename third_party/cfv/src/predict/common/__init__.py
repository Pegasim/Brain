"""Tahmin yaklaşımlarının ortak altyapısı.

- `sessions`: WhatsApp mesajlarından sohbet oturumları (30 dk sessizlik → yeni oturum)
- `targets`: günlük hedef tablosu (her yaklaşımın doldurduğu ortak tablo) + t gününde bilinenler
- `baselines`: "yarın = dün" ve "son 4 aynı günün ortalaması"
- `evaluate`: walk-forward değerlendirme (tahmin dosyası → metrikler, rakiplere oranla)

Çıktı: data/processed/predict/common.sqlite (session, day, meta).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# Değerlendirme dönemleri: dev'de ayar yapılır, test yalnız rapor için.
SPLITS = {
    "dev": ("2025-05-01", "2025-10-31"),
    "test": ("2025-11-01", None),  # None → tablodaki son tam gün
}


def data_dir() -> Path:
    return Path(os.environ.get("CFV_DATA", REPO_ROOT / "data"))


def cfv_db() -> Path:
    return data_dir() / "processed" / "cfv.sqlite"


def out_dir(name: str = "") -> Path:
    p = data_dir() / "processed" / "predict" / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def common_db() -> Path:
    return out_dir() / "common.sqlite"


def ro(path: Path) -> sqlite3.Connection:
    """Salt-okur bağlantı (ham/işlenmiş veriye asla yazmayız)."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
