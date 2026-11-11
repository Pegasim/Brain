"""Google Takvim → data/raw/google-calendar/ sürekli (salt-okur) senkron.

Her çalıştırmada (gog v0.11; sunucuda günde 4 kez, src/systemd/system/cfv-gcal-sync.*):
1. Tüm takvimler tam çekilir (tekrarlar açılmış halde, 2010 → bugün+400 gün), önceki anlık
   görüntüyle karşılaştırılır, fark `changes.jsonl`'a eklenir (silinen olayların tam içeriği
   dahil — arşiv kaybolmaz), değiştiyse `events.json` atomik olarak yenilenir. Eski events.json'ın
   tarihli kopyası src/gsync/snapshot.sh ile history/'ye alınır.
2. Değişiklik olduysa (ops.) cfv.sqlite'a import + klasör ağacı yeniden üretilir.

Hesap: CFV_GCAL_ACCOUNT ya da data/config/gcal-account (koda kişisel veri girmez).
gog, izin listeli src/gsync/gog-ro üzerinden `--enable-commands=calendar --no-input` ile
çağrılır: yalnız `calendar calendars` ve `calendar events` geçer, yazma isteği imkânsız.
Ağ yoksa / token düştüyse: log'a yazar, hiçbir şeye dokunmadan 0 ile çıkar (timer sessiz kalır).
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from cfv.db import REPO_ROOT, data_dir

GOG = os.environ.get("CFV_GOG", str(REPO_ROOT / "src" / "gsync" / "gog-ro"))
WINDOW_START = "2010-01-01"
WINDOW_AHEAD_DAYS = 400
MAX_REMOVED_RATIO = 0.2  # tam çekimde olayların %20'sinden fazlası "silindi" görünürse yazma

log = logging.getLogger("cfv.gcal")


class GogError(RuntimeError):
    pass


def account() -> str:
    a = os.environ.get("CFV_GCAL_ACCOUNT")
    if a is None:
        p = data_dir() / "config" / "gcal-account"
        a = p.read_text() if p.exists() else ""
    a = a.strip()
    if not a:
        raise GogError("takvim hesabı yok (CFV_GCAL_ACCOUNT ya da data/config/gcal-account)")
    return a


def out_dir() -> Path:
    return data_dir() / "raw" / "google-calendar"


def log_path() -> Path:
    return data_dir() / "logs" / "gcal-sync.log"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def gog(*args: str, timeout: int = 180) -> dict:
    cmd = [GOG, "--enable-commands=calendar", "--no-input", "--color=never",
           "--account", account(), "calendar", *args, "--json"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise GogError(f"{args[0]}: {type(e).__name__}: {e}") from None
    err = p.stderr.strip()
    if p.returncode != 0 or "error" in err.lower():
        raise GogError(f"{args[0]} rc={p.returncode}: {err[:400]}")
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError as e:
        raise GogError(f"{args[0]}: JSON okunamadı: {e}") from None


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def calendars() -> list[dict]:
    d = gog("calendars", "--max", "250")
    cals = d.get("calendars", d if isinstance(d, list) else [])
    return [{k: c.get(k) for k in ("id", "summary", "accessRole", "timeZone", "primary")} for c in cals]


def full_fetch(cal_ids: list[str], now: dt.datetime) -> list[dict]:
    to = (now + dt.timedelta(days=WINDOW_AHEAD_DAYS)).date().isoformat()
    events = []
    for cid in cal_ids:
        d = gog("events", cid, "--from", WINDOW_START, "--to", to, "--all-pages", "--max", "2500")
        if any(d.get("nextPageTokens") or []) or d.get("nextPageToken"):
            raise GogError(f"{cid}: sayfalama tamamlanmadı")
        for e in d.get("events") or []:
            e["CalendarID"] = cid
            events.append(e)
    return events


def _key(e: dict) -> str:
    return f"{e['CalendarID']}/{e['id']}"


def diff(old: list[dict], new: list[dict]) -> dict:
    o = {_key(e): e for e in old}
    n = {_key(e): e for e in new}
    return {
        "added": sorted(k for k in n if k not in o),
        "changed": sorted(k for k in n if k in o and n[k].get("etag") != o[k].get("etag")),
        "removed": [o[k] for k in sorted(o) if k not in n],
    }


def sync(*, allow_mass_removal: bool = False) -> dict:
    """Bir senkron turu. Dönen sözlük: ne yapıldığı. GogError dışarı sızmaz → 'error'."""
    root = out_dir()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    state_p, events_p = root / "state.json", root / "events.json"
    lock = open(root / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return {"status": "busy"}

    state = _read_json(state_p, {})
    now = _now()
    try:
        acc = account()
        cals = calendars()
        cal_ids = [c["id"] for c in cals]
        reason = "tam çekim"
        new = full_fetch(cal_ids, now)
    except GogError as e:
        state.update(last_error=f"{_iso(now)} {e}", failures=state.get("failures", 0) + 1)
        _write_json(state_p, state)
        return {"status": "error", "error": str(e)}

    old = _read_json(events_p, {}).get("events", [])
    d = diff(old, new)
    if old and len(d["removed"]) > MAX_REMOVED_RATIO * len(old) and not allow_mass_removal:
        msg = f"{len(d['removed'])}/{len(old)} olay silinmiş görünüyor; yazılmadı (--allow-mass-removal)"
        state.update(last_error=f"{_iso(now)} {msg}", failures=state.get("failures", 0) + 1)
        _write_json(state_p, state)
        return {"status": "error", "error": msg}

    changed = bool(d["added"] or d["changed"] or d["removed"])
    if changed or not events_p.exists():
        _write_json(events_p, {"fetched_at": _iso(now), "account": acc,
                               "window": [WINDOW_START, (now + dt.timedelta(days=WINDOW_AHEAD_DAYS)).date().isoformat()],
                               "calendars": cals, "events": new})
        with open(root / "changes.jsonl", "a") as f:
            f.write(json.dumps({"at": _iso(now), "reason": reason, "total": len(new), **d},
                               ensure_ascii=False) + "\n")
    state.update(last_check=_iso(now), last_full=_iso(now), last_ok=_iso(now), last_error=None,
                 failures=0, total=len(new), calendars=len(cals))
    _write_json(state_p, state)
    return {"status": "changed" if changed else "unchanged", "reason": reason, "total": len(new),
            "added": len(d["added"]), "changed": len(d["changed"]), "removed": len(d["removed"])}


def setup_logging() -> None:
    p = log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    h = logging.FileHandler(p)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def run(*, force_full=True, allow_mass_removal=False, ingest=True) -> int:
    """systemd timer'ın çağırdığı giriş. Her durumda 0 döner; ayrıntı log'da.
    force_full: CLI uyumu için duruyor; her tur zaten tam çekim."""
    os.umask(0o077)
    setup_logging()
    try:
        r = sync(allow_mass_removal=allow_mass_removal)
    except Exception as e:  # beklenmeyen hata da timer'ı gürültüye boğmasın
        log.exception("beklenmeyen hata: %s", e)
        return 0
    if r["status"] == "error":
        log.warning("senkron yapılamadı: %s", r["error"])
        return 0
    if r["status"] == "busy":
        log.info("başka bir senkron sürüyor, atlandı")
        return 0
    if r["status"] == "unchanged":
        log.info("değişiklik yok%s", f" (tam çekim: {r['total']} olay)" if "total" in r else "")
        return 0
    log.info("güncellendi (%s): +%d ~%d -%d, toplam %d", r["reason"], r["added"], r["changed"],
             r["removed"], r["total"])
    if ingest:
        try:
            from cfv import db, tree
            from cfv.sources import calendar_gcal
            conn = db.connect(db.default_db_path())
            conn.executescript(db.schema_sql())
            with conn:
                stats = calendar_gcal.ingest(conn)
            from cfv import link
            link.run_all(conn)
            tree.build(conn, data_dir() / "processed" / "timeline")
            conn.close()
            log.info("cfv.sqlite + timeline güncellendi: %s", stats)
        except Exception as e:
            log.exception("import/ağaç hatası: %s", e)
    return 0
