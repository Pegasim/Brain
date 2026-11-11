"""Hiyerarşik raporlar (data/processed/reports/**/index.md, src/compact üretir) → 'report' event'leri.

Raporlar aranabilir olsun diye DB'ye girer: "ne zaman şu işle uğraştım" sorusu en iyi buradan
yanıtlanır (rapor, aralığın kaynaklarını zaten birleştirmiş halde). Her rapor kapsadığı aralığın
ilk gününe yerleşir; katman (yıl/ay/hafta/gün/saat) payload'da.
"""

from __future__ import annotations

import datetime as dt
import re

from cfv.ingest import Batch
from cfv import db
from cfv.timeutil import DEFAULT_TZ

IMPORTER = "cfv.sources.reports@1"


def _field(text: str, key: str) -> str | None:
    m = re.search(rf"^{key}:\s*(.+?)\s*$", text, re.M)
    return m.group(1) if m else None


def ingest(conn) -> dict:
    root = db.data_dir() / "processed" / "reports"
    files = sorted(root.rglob("index.md"))
    if not files:
        raise FileNotFoundError(root)
    batch = Batch(conn, "reports", root, IMPORTER)
    n = 0
    for f in files:
        text = f.read_text()
        rng = _field(text, "range")
        if not rng:
            continue
        d = dt.date.fromisoformat(rng.split()[0])
        title = re.search(r"^# .*?—\s*(.+)$", text, re.M)
        projects = _field(text, "projects") or ""
        node = str(f.parent.relative_to(root)) if f.parent != root else "kök"
        batch.event(node, "report", dt.datetime.combine(d, dt.time(0), DEFAULT_TZ),
                    time_quality="day", day=d.isoformat(),
                    title=title.group(1).strip() if title else node,
                    body=text.split("\n---\n", 1)[-1],
                    payload={"path": str(f), "node": node, "level": _field(text, "level"), "range": rng,
                             "projects": [p.strip() for p in projects.strip("[]").split(",") if p.strip()]})
        n += 1
    return {"reports": n}
