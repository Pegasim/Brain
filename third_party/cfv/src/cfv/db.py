"""Zaman çizelgesi veritabanı: yol çözümü ve şema kurulumu."""

from __future__ import annotations

import os
import sqlite3
from importlib.resources import files
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    """Kişisel veri kökü. CFV_DATA ile değiştirilebilir (testler geçici dizin kullanır)."""
    return Path(os.environ.get("CFV_DATA", REPO_ROOT / "data"))


def default_db_path() -> Path:
    return Path(os.environ["CFV_DB"]) if os.environ.get("CFV_DB") else data_dir() / "processed" / "cfv.sqlite"


def schema_sql() -> str:
    return files("cfv").joinpath("schema.sql").read_text(encoding="utf-8")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(path: Path | None = None) -> Path:
    path = path or default_db_path()
    with connect(path) as conn:
        conn.executescript(schema_sql())
    return path


# Bu türler dışa açılan hiçbir çıktıya (ağaç, grafik, site, raporlar) girmez. Rüya metni zaten
# event.body'de değil, yalnız `dream` tablosunda; varlığı ve kişi önerisi de dışarıda kalsın.
PRIVATE_KINDS = ("dream",)

_TR_FOLD = str.maketrans({"ı": "i", "İ": "i"})


def fold_tr(text: str) -> str:
    """FTS sorgusunu indeksle aynı biçimde katla (ı/İ → i; gerisini unicode61 halleder)."""
    return text.translate(_TR_FOLD)
