"""Gmail → data/raw/gmail/<hesap>/ salt-okur, artımlı senkron (gövde yok, yalnızca snippet).

Her hesap için:
1. `gog gmail messages search` ile mesaj id'leri listelenir. İlk çekimde ay ay
   (`after:`/`before:` unix saniye; dakikalık kotaya sığsın), sonraki çekimlerde son çekimden 2 gün öncesinden beri.
2. Yeni id'ler için `gog gmail get --format metadata --json` (tek mesaj, gövdesiz): zaman, etiketler,
   snippet, thread, From/To/Cc/Subject.
3. `messages.jsonl` (id'ye göre tekil, zamana göre sıralı) atomik yeniden yazılır; `state.json`.

gog (v0.11), izin listeli src/gsync/gog-ro üzerinden `--enable-commands=gmail --no-input` ile
çağrılır: yalnız labels list / messages search / get geçer; gönderme/silme/etiketleme imkânsız.
Sunucuda günde 1 kez (src/systemd/system/cfv-gmail-sync.*); messages.jsonl'ın tarihli kopyası
src/gsync/snapshot.sh ile history/'ye alınır.
Gmail kotası (dakikalık) dolarsa bekleyip yeniden dener. Ağ yoksa / token düştüyse: log'a yazar,
0 ile çıkar. Etiket değişiklikleri (okundu, arşiv…) eski mesajlara geri yansıtılmaz.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import logging
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cfv.db import REPO_ROOT, data_dir

GOG = os.environ.get("CFV_GOG", str(REPO_ROOT / "src" / "gsync" / "gog-ro"))
FIRST_YEAR = 2004  # Gmail'in açılışı
OVERLAP = dt.timedelta(days=2)
WORKERS = 4
RATE_WAIT_S = 65
RATE_TRIES = 8

log = logging.getLogger("cfv.gmail")


class GogError(RuntimeError):
    pass


def accounts() -> list[str]:
    """Hesaplar: CFV_GMAIL_ACCOUNTS (virgüllü) ya da data/config/gmail-accounts (satır başına bir)."""
    raw = os.environ.get("CFV_GMAIL_ACCOUNTS")
    if raw is None:
        p = data_dir() / "config" / "gmail-accounts"
        raw = p.read_text() if p.exists() else ""
    return [a.strip() for a in raw.replace(",", "\n").splitlines() if a.strip()]


def out_dir() -> Path:
    return data_dir() / "raw" / "gmail"


def log_path() -> Path:
    return data_dir() / "logs" / "gmail-sync.log"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def gog(account: str, *args: str, timeout: int = 300) -> dict:
    cmd = [GOG, "--enable-commands=gmail", "--no-input", "--color=never",
           "--account", account, "gmail", *args]
    for _ in range(RATE_TRIES):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError) as e:
            raise GogError(f"{args[0]}: {type(e).__name__}: {e}") from None
        err = p.stderr.strip()
        if "rateLimitExceeded" in err or "rate_limited" in err:
            time.sleep(RATE_WAIT_S)  # dakikalık kota: bekle, yeniden dene
            continue
        if p.returncode != 0 or "error" in err.lower():
            raise GogError(f"{args[0]} rc={p.returncode}: {err[:400]}")
        try:
            return json.loads(p.stdout)
        except json.JSONDecodeError as e:
            raise GogError(f"{args[0]}: JSON okunamadı: {e}") from None
    raise GogError(f"{args[0]}: kota aşımı sürüyor ({RATE_TRIES} deneme)")


def list_ids(account: str, query: str) -> list[str]:
    d = gog(account, "messages", "search", query, "--all", "--max", "500", "--json")
    if d.get("nextPageToken"):
        raise GogError(f"{query}: sayfalama tamamlanmadı")
    return [m["id"] for m in d.get("messages") or []]


def fetch(account: str, mid: str, labels: dict[str, str]) -> dict:
    d = gog(account, "get", mid, "--format", "metadata", "--json")
    m = d.get("message", d)
    h = {}
    for x in m.get("payload", {}).get("headers", []):
        if x.get("name"):  # gog v0.11 boş değerli başlıkta "value" alanını atlıyor
            h.setdefault(x["name"].lower(), x.get("value"))
    return {
        "id": m["id"], "thread": m.get("threadId"),
        "time": _iso(dt.datetime.fromtimestamp(int(m["internalDate"]) / 1000, dt.timezone.utc)),
        "labels": [labels.get(lid, lid) for lid in m.get("labelIds") or []],
        "from": h.get("from"), "to": h.get("to"), "cc": h.get("cc"),
        "subject": h.get("subject"), "snippet": m.get("snippet"),
    }


def _write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_jsonl(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    return {m["id"]: m for m in map(json.loads, path.read_text().splitlines()) if m}


def _save(path: Path, msgs: dict[str, dict]) -> None:
    rows = sorted(msgs.values(), key=lambda m: (m["time"], m["id"]))
    _write(path, "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in rows))


def _windows(state: dict, now: dt.datetime) -> list[str]:
    """Çekilecek sorgular: ilk seferde ay ay, sonra son çekimden beri."""
    if state.get("last_fetch"):
        since = dt.datetime.fromisoformat(state["last_fetch"]) - OVERLAP
        return [f"in:anywhere after:{int(since.timestamp())}"]
    q = []
    for y in range(FIRST_YEAR, now.year + 1):
        for mo in range(1, 13):
            a = dt.datetime(y, mo, 1, tzinfo=dt.timezone.utc)
            b = dt.datetime(y + mo // 12, mo % 12 + 1, 1, tzinfo=dt.timezone.utc)
            if a <= now:
                q.append(f"in:anywhere after:{int(a.timestamp())} before:{int(b.timestamp())}")
    return q


def sync_account(account: str) -> dict:
    root = out_dir() / account
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir(), 0o700)
    os.chmod(root, 0o700)
    state_p, msgs_p = root / "state.json", root / "messages.jsonl"
    lock = open(root / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return {"status": "busy"}

    state = json.loads(state_p.read_text()) if state_p.exists() else {}
    msgs = _read_jsonl(msgs_p)
    now = _now()
    added = 0
    try:
        labels = {x["id"]: x["name"] for x in gog(account, "labels", "list", "--json").get("labels", [])}
        for q in _windows(state, now):
            new = [i for i in list_ids(account, q) if i not in msgs]
            if not new:
                continue
            with ThreadPoolExecutor(WORKERS) as ex:
                for m in ex.map(lambda i: fetch(account, i, labels), new):
                    msgs[m["id"]] = m
            _save(msgs_p, msgs)  # her pencereden sonra: yarıda kalırsa kaldığı yerden sürer
            added += len(new)
            log.info("%s: %s → +%d", account, q, len(new))
    except GogError as e:
        state.update(last_error=f"{_iso(now)} {e}", failures=state.get("failures", 0) + 1)
        _write(state_p, json.dumps(state, indent=1))
        return {"status": "error", "error": str(e), "added": added}

    if not msgs_p.exists():
        _save(msgs_p, msgs)
    state.update(account=account, last_fetch=_iso(now), last_error=None, failures=0, total=len(msgs))
    _write(state_p, json.dumps(state, indent=1))
    return {"status": "changed" if added else "unchanged", "added": added, "total": len(msgs)}


def setup_logging() -> None:
    p = log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    h = logging.FileHandler(p)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def run(*, ingest: bool = True) -> int:
    """Timer/CLI girişi. Her durumda 0 döner; ayrıntı log'da."""
    os.umask(0o077)
    setup_logging()
    changed = False
    for account in accounts():
        try:
            r = sync_account(account)
        except Exception as e:
            log.exception("%s: beklenmeyen hata: %s", account, e)
            continue
        if r["status"] == "error":
            log.warning("%s: senkron yapılamadı (+%d kaydedildi): %s", account, r["added"], r["error"])
        elif r["status"] == "busy":
            log.info("%s: başka bir senkron sürüyor, atlandı", account)
        else:
            log.info("%s: +%d, toplam %d", account, r["added"], r["total"])
        changed |= r.get("added", 0) > 0
    if ingest and changed:
        try:
            from cfv import db
            from cfv.sources import gmail
            conn = db.connect(db.default_db_path())
            conn.executescript(db.schema_sql())
            with conn:
                stats = gmail.ingest(conn)
            conn.close()
            log.info("cfv.sqlite güncellendi: %s", stats)
        except Exception as e:
            log.exception("import hatası: %s", e)
    return 0
