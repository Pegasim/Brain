"""Gmail (gog metadata + snippet) → 'email' event'leri.

Ham veri: data/raw/gmail/<hesap>/messages.jsonl (bkz. cfv.gmail_sync). Hesap başına bir kaynak.
SPAM/TRASH/DRAFT etiketliler atlanır; SENT etiketli = giden.
Kişiler e-posta adresiyle bağlanır: gelende gönderen ('sender'), gidende alıcılar ('recipient').
"""

from __future__ import annotations

import datetime as dt
import json
from email.utils import getaddresses
from pathlib import Path

from cfv import db
from cfv.ingest import Batch

IMPORTER = "cfv.sources.gmail@1"
SKIP_LABELS = {"SPAM", "TRASH", "DRAFT"}


def default_root() -> Path:
    return db.data_dir() / "raw" / "gmail"


def _addrs(*headers: str | None) -> list[tuple[str, str]]:
    return [(n.strip(), a.strip().lower()) for n, a in getaddresses([h for h in headers if h]) if "@" in a]


def ingest(conn, root: Path | None = None) -> dict:
    root = root or default_root()
    paths = sorted(root.glob("*/messages.jsonl"))
    if not paths:
        raise FileNotFoundError(root / "*/messages.jsonl")
    stats = {"emails": 0, "skipped": 0}
    for path in paths:
        account = path.parent.name
        state_p = path.with_name("state.json")
        state = json.loads(state_p.read_text()) if state_p.exists() else {}
        batch = Batch(conn, "gmail", path, IMPORTER, snapshot=state.get("last_fetch"))
        for line in path.read_text().splitlines():
            m = json.loads(line)
            labels = m.get("labels") or []
            if SKIP_LABELS & set(labels):
                stats["skipped"] += 1
                continue
            out = "SENT" in labels
            to = ", ".join(x for x in (m.get("to"), m.get("cc")) if x)
            body = "\n".join(x for x in (m.get("from") and f"Kimden: {m['from']}",
                                         to and f"Kime: {to}", m.get("snippet")) if x)
            eid = batch.event(m["id"], "email", dt.datetime.fromisoformat(m["time"]),
                              title=m.get("subject") or None, body=body or None,
                              payload={"account": account, "from": m.get("from"), "to": to or None,
                                       "labels": labels, "thread": m.get("thread"),
                                       "direction": "out" if out else "in"})
            people = _addrs(m.get("to"), m.get("cc")) if out else _addrs(m.get("from"))
            for name, addr in people:
                if addr != account.lower():
                    pid = batch.person(alias=addr, name=name or addr, source_kind="email")
                    batch.link_person(eid, pid, "recipient" if out else "sender")
            stats["emails"] += 1
    return stats
