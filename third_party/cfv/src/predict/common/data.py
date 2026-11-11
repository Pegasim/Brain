"""common.sqlite'ı yaz ve oku."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from . import cfv_db, common_db, ro
from . import sessions as S
from . import targets as T

SCHEMA = f"""
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE session (id INTEGER PRIMARY KEY, chat_jid TEXT, chat TEXT, chat_type TEXT, day TEXT,
  start_utc TEXT, end_utc TEXT, n_msgs INTEGER, n_me INTEGER, people TEXT);
CREATE INDEX session_day ON session(day);
CREATE TABLE day (day TEXT PRIMARY KEY, dow INTEGER,
  {", ".join(f"{k} REAL" for k in T.COUNTS)},
  {", ".join(f"{k} TEXT" for k in T.SETS + T.TEXT)});
"""


def build(src: Path | None = None, dst: Path | None = None, last: str | None = None) -> dict:
    src, dst = src or cfv_db(), dst or common_db()
    with ro(src) as conn:
        sess = S.build(conn)
        rows, meta = T.build(conn, sess, last)
    tmp = dst.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    out = sqlite3.connect(tmp)
    out.executescript(SCHEMA)
    out.executemany("INSERT INTO session VALUES (NULL,?,?,?,?,?,?,?,?,?)",
                    [(s["chat_jid"], s["chat"], s["chat_type"], s["day"], s["start_utc"], s["end_utc"],
                      s["n_msgs"], s["n_me"], json.dumps(s["people"])) for s in sess])
    cols = ["day", "dow"] + T.COUNTS + T.SETS + T.TEXT
    out.executemany(f"INSERT INTO day VALUES ({','.join('?' * len(cols))})",
                    [[json.dumps(r[c], ensure_ascii=False) if c in T.SETS and c in r else r.get(c)
                      for c in cols] for r in rows])
    meta |= {"sessions": str(len(sess)), "gap_min": str(S.GAP_S // 60)}
    out.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
    out.commit()
    out.close()
    os.replace(tmp, dst)
    return meta


def load_days(path: Path | None = None) -> dict[str, dict]:
    """gün → {hedef: değer}; NULL hedefler anahtarda yok, kümeler list."""
    with ro(path or common_db()) as conn:
        conn.row_factory = sqlite3.Row
        out = {}
        for r in conn.execute("SELECT * FROM day ORDER BY day"):
            d = {k: (json.loads(v) if k in T.SETS else v) for k, v in dict(r).items() if v is not None}
            for k in T.COUNTS:
                if k in d and float(d[k]).is_integer():
                    d[k] = int(d[k])
            out[r["day"]] = d
        return out


def history(by_day: dict[str, dict], day: str) -> dict[str, dict]:
    """Walk-forward: yalnız t'den önceki günler."""
    return {d: r for d, r in by_day.items() if d < day}
