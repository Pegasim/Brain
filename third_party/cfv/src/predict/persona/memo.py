"""Geçmiş günlerin mini-raporları: yalnız o günün ham dökümünden (`compact day`).

Yazımı Haiku ajanları yapar (jobs.py; GPU'da LLM yok). Önbellek: data/processed/predict/persona/memo/<gün>.md
Eski yerel gemma raporları persona_gemma/memo/'da (karşılaştırma için).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from common import REPO_ROOT, data_dir, out_dir

PROMPT = (Path(__file__).parent / "prompts" / "memo.md").read_text()


def labels() -> str:
    txt = (data_dir() / "config" / "day-context.md").read_text()
    return ", ".join(re.findall(r"^- (\w+) —", txt, re.M))


def dump(day: str) -> str:
    """Ham döküm; ödevlerin [x]/[ ] bayrağı notun SON hâlidir (t'den sonra işaretlenmiş olabilir) → maskele."""
    out = subprocess.run([str(REPO_ROOT / "bin" / "compact"), "day", day],
                         check=True, capture_output=True, text=True, cwd=REPO_ROOT).stdout
    out = re.sub(r"^## Ödev/yapılacaklar \(.*\)$", "## Ödev/yapılacaklar (planlanan; yapılıp yapılmadığı bilinmiyor)", out, flags=re.M)
    return re.sub(r"^- \[[x ]\] ", "- ", out, flags=re.M)


def prompt(day: str) -> str:
    return PROMPT.format(labels=labels(), day=day, input=dump(day))


def path(day: str, name: str = "persona") -> Path:
    return out_dir(f"{name}/memo") / f"{day}.md"


def memo(day: str) -> str:
    p = path(day)
    if not p.exists():
        raise FileNotFoundError(f"mini-rapor yok: {day} (önce `jobs memo` + Haiku ajanları)")
    return p.read_text()
