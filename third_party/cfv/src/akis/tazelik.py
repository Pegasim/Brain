"""Tazelik denetimi: kaynak başına son ham veri + DB'deki son olay; eşik aşılırsa bp msg server-cfv.

Eşikler (server-cfv, 2026-11-10): foto > 2 gün, takvim > 1 gün, gmail > 2 gün, iPhone yedeği > 8 gün;
ham veride olup DB'ye girmemiş öğe > 1 gün (ingest bozuk). Çekimin kendi hatası (token, ağ) gsync-check / icloud
kontrol'ün işi; burası "ham veri geldi mi, DB'ye girdi mi"ye bakar.
Aynı sorun tekrar ederse ilk gün + 3 günde bir hatırlatılır (data/logs/akis-uyari.json).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
from pathlib import Path

from cfv import db
from cfv.ingest import DONE_MARK, iphone_extract, snapshot_date

from akis import sayim

DAY = dt.timedelta(days=1)
LIMITS = {"foto": 2 * DAY, "takvim": 1 * DAY, "gmail": 2 * DAY, "iphone": 8 * DAY}
LAG = 1 * DAY
REMIND = 3 * DAY
BACKUP_DIR_MAX = 20 * 2**30      # processed/yedek + photos/yedek-gunluk toplamı
DISK_FREE_MIN = 100 * 2**30      # depo1 boş alan
FOTO_DURUM = Path(os.environ.get("CFV_FOTO_DURUM", "/mnt/depo1/foto/_durum/gunluk.tsv"))


def _t(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    s = s.strip().replace(" ", "T")
    if len(s) == 10:
        s += "T00:00:00"
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if t.tzinfo is None:  # gunluk.tsv yerel saat (Europe/Istanbul, sunucu saati)
        t = t.astimezone()
    return t.astimezone(dt.timezone.utc)


def _d(t: dt.datetime | None) -> str:
    """yerel (sunucu saati, Europe/Istanbul) gün"""
    return t.astimezone().strftime("%Y-%m-%d") if t else "-"


def _json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _age(now, t):
    return None if t is None else now - t


def _fmt_age(d: dt.timedelta | None) -> str:
    if d is None:
        return "yok"
    h = d.total_seconds() / 3600
    return f"{h:.0f} sa" if h < 48 else f"{h / 24:.1f} gün"


def _dir_size(*ds: Path) -> int:
    n = 0
    for d in ds:
        for dp, _, fs in os.walk(d):
            for f in fs:
                try:
                    n += os.lstat(os.path.join(dp, f)).st_size
                except OSError:
                    pass
    return n


def evaluate(cfv_db: Path | None = None, now: dt.datetime | None = None) -> dict:
    """Döner: {'kaynaklar': {ad: {...}}, 'sorunlar': [(anahtar, metin)]} — yazmaz, mesaj atmaz."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cfv_db = cfv_db or db.default_db_path()
    data = db.data_dir()
    last = sayim.last_events(cfv_db, now)
    srcs = {k: (rp, snap, imp) for k, rp, snap, imp in sayim.sources(cfv_db)}
    out, problems = {}, []

    # --- foto: icloud kontrolünün günlük satırı (son 'tamam' tur + en yeni çekim)
    rows = []
    if FOTO_DURUM.exists():
        lines = FOTO_DURUM.read_text().splitlines()
        hdr = lines[0].split("\t") if lines else []
        rows = [dict(zip(hdr, line.split("\t"))) for line in lines[1:]]
    ok = [r for r in rows if r.get("sonuc") == "tamam"]
    fetched = _t(ok[-1]["zaman"]) if ok else None
    newest = _t(ok[-1].get("en_yeni_cekim")) if ok else None
    db_last = _t(last.get("iphone-photos"))
    out["foto"] = {"son_cekim_turu": fetched, "ham_en_yeni": newest, "db_son_olay": db_last}
    if _age(now, fetched) is None or _age(now, fetched) > LIMITS["foto"]:
        problems.append(("foto-cekim", f"foto: son başarılı çekim turu {_fmt_age(_age(now, fetched))} önce"))
    if newest and now - newest > LIMITS["foto"] + DAY:  # en_yeni_cekim gün hassasiyetinde
        problems.append(("foto-yeni", f"foto: arşivdeki en yeni çekim {_d(newest)} (> 2 gün)"))
    if newest and (db_last is None or db_last < newest - LAG):
        problems.append(("foto-db", f"foto: arşivde {_d(newest)} var, DB'deki son foto "
                                    f"{_d(db_last)}" if db_last else "foto: DB'de foto yok"))

    # --- takvim (google): state.json last_ok + events.json fetched_at ↔ DB snapshot
    st = _json(data / "raw" / "google-calendar" / "state.json")
    last_ok = _t(st.get("last_ok"))
    snap_db = _t(srcs.get("calendar-google", (None, None, None))[1])
    out["takvim"] = {"son_cekim": last_ok, "db_snapshot": snap_db, "db_son_olay": _t(last.get("calendar-google"))}
    if _age(now, last_ok) is None or _age(now, last_ok) > LIMITS["takvim"]:
        problems.append(("takvim-cekim", f"takvim: son başarılı çekim {_fmt_age(_age(now, last_ok))} önce"))
    if last_ok and (snap_db is None or snap_db < last_ok - LAG):
        problems.append(("takvim-db", f"takvim: ham {last_ok:%Y-%m-%d %H:%M}Z, DB'deki çekim "
                                      f"{snap_db:%Y-%m-%d %H:%M}Z" if snap_db else "takvim: DB'de yok"))

    # --- gmail: hesap başına last_fetch + ham en yeni mail ↔ DB son mail
    gm = {}
    newest_mail = None
    for st_p in sorted((data / "raw" / "gmail").glob("*/state.json")):
        acct = st_p.parent.name
        s = _json(st_p)
        lf = _t(s.get("last_fetch"))
        gm["…" + acct.split("@")[0][-2:]] = lf  # hesap adı log'a tam yazılmaz
        if _age(now, lf) is None or _age(now, lf) > LIMITS["gmail"]:
            problems.append((f"gmail-cekim-{acct}", f"gmail (…{acct.split('@')[0][-2:]}): son çekim "
                                                    f"{_fmt_age(_age(now, lf))} önce"))
        m = st_p.parent / "messages.jsonl"
        if m.exists():
            with open(m) as f:
                for line in f:
                    try:
                        t = _t(json.loads(line).get("time"))
                    except (ValueError, AttributeError):
                        continue
                    if t and t <= now and (newest_mail is None or t > newest_mail):
                        newest_mail = t
    db_mail = _t(last.get("gmail"))
    out["gmail"] = {"son_cekim": gm, "ham_en_yeni": newest_mail, "db_son_olay": db_mail}
    if newest_mail and now - newest_mail > LIMITS["gmail"]:
        problems.append(("gmail-yeni", f"gmail: en yeni mail {_fmt_age(now - newest_mail)} önce"))
    if newest_mail and (db_mail is None or db_mail < newest_mail - LAG):
        problems.append(("gmail-db", f"gmail: ham {_d(newest_mail)}, DB {_d(db_mail)}"
                                     if db_mail else "gmail: DB'de mail yok"))

    # --- iPhone yedeği: en yeni .tamam'lı extract; işaretli ilk yedek gelene kadar "bekleniyor"
    ext = iphone_extract()
    marked = (ext / DONE_MARK).exists()
    ext_t = _t(snapshot_date(ext)) if ext.exists() else None
    out["iphone"] = {"extract": ext.name, "tarih": ext_t, "durum": "otomatik" if marked else "bekleniyor (elle yedek)",
                     "db": {k: srcs.get(k, (None, None, None))[1]
                            for k in ("apple-notes", "whatsapp", "zepp", "calendar-ios")}}
    if marked and (ext_t is None or now - ext_t > LIMITS["iphone"] + DAY):
        problems.append(("iphone-yedek", f"iPhone yedeği: en yeni {ext.name} (> 8 gün)"))
    if marked:
        stale = [k for k, snap in out["iphone"]["db"].items() if snap and snap < snapshot_date(ext)]
        if stale:
            problems.append(("iphone-db", f"iPhone: {ext.name} var ama DB'de eski snapshot: {', '.join(stale)}"))

    # --- yer: yedek klasörleri ve depo1 boşluğu
    bsize = _dir_size(data / "processed" / "yedek", data / "processed" / "photos" / "yedek-gunluk")
    free = shutil.disk_usage(data.resolve()).free
    out["yer"] = {"yedek_bayt": bsize, "bos_bayt": free}
    if bsize > BACKUP_DIR_MAX:
        problems.append(("yer-yedek", f"yedekler {bsize / 2**30:.1f} GB (> 20 GB); otomatik silme yok, karar gerek"))
    if free < DISK_FREE_MIN:
        problems.append(("yer-disk", f"depo1 boş alan {free / 2**30:.0f} GB (< 100 GB)"))
    return {"kaynaklar": out, "sorunlar": problems}


# --- uyarı ---------------------------------------------------------------------------------------------

def state_path() -> Path:
    return db.data_dir() / "logs" / "akis-uyari.json"


def due(problems: list[tuple[str, str]], now: dt.datetime | None = None, *, save: bool = True) -> list[str]:
    """Bugün gönderilecek sorunlar: yeni çıkanlar + son hatırlatmadan 3 gün geçenler. Düzelenler durumdan düşer."""
    now = now or dt.datetime.now(dt.timezone.utc)
    st = _json(state_path())
    new_st, send = {}, []
    for key, text in problems:
        prev = st.get(key)
        sent = _t(prev["son_gonderim"]) if prev else None
        if sent is None or now - sent >= REMIND - dt.timedelta(hours=2):
            send.append(text)
            sent = now
        new_st[key] = {"ilk": prev["ilk"] if prev else now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "son_gonderim": sent.strftime("%Y-%m-%dT%H:%M:%SZ"), "metin": text}
    if save:
        p = state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.yeni")
        tmp.write_text(json.dumps(new_st, ensure_ascii=False, indent=1))
        tmp.replace(p)
    return send


def alert(msg: str, *, title: str = "cfv gece turu") -> bool:
    """bp msg server-cfv; olmazsa journal + laptop-notify. bp gönderdiyse True."""
    msg = msg[:1500]
    try:
        sent = subprocess.run(["bp", "msg", "server-cfv", msg], check=False, capture_output=True,
                              timeout=120).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        sent = False
    if not sent:
        subprocess.run(["logger", "-t", "cfv-akis", msg], check=False)
        subprocess.run(["laptop-notify", title, msg[:400], "normal"], check=False, capture_output=True)
    return sent


def report(ev: dict) -> str:
    """İnsan okunur kısa durum (cfv status için)."""
    k = ev["kaynaklar"]
    now = dt.datetime.now(dt.timezone.utc)

    def a(t):
        return _fmt_age(_age(now, t)) if isinstance(t, dt.datetime) else "yok"

    lines = [
        f"foto     çekim turu {a(k['foto']['son_cekim_turu'])} önce · arşiv en yeni "
        f"{_d(k['foto']['ham_en_yeni'])} · DB son foto {a(k['foto']['db_son_olay'])} önce"
        if k["foto"]["ham_en_yeni"] else f"foto     çekim durumu okunamadı ({FOTO_DURUM})",
        f"takvim   çekim {a(k['takvim']['son_cekim'])} önce · DB'deki çekim {a(k['takvim']['db_snapshot'])} önce",
        f"gmail    çekim " + ", ".join(f"{h} {a(t)}" for h, t in k["gmail"]["son_cekim"].items())
        + f" önce · ham en yeni {a(k['gmail']['ham_en_yeni'])} · DB en yeni {a(k['gmail']['db_son_olay'])} önce",
        f"iphone   {k['iphone']['extract']} ({k['iphone']['durum']}, {a(k['iphone']['tarih'])} önce)",
        f"yer      yedekler {k['yer']['yedek_bayt'] / 2**30:.2f} GB · depo1 boş {k['yer']['bos_bayt'] / 2**30:.0f} GB",
    ]
    if ev["sorunlar"]:
        lines.append("SORUN:  " + "\n        ".join(t for _, t in ev["sorunlar"]))
    else:
        lines.append("sorun yok")
    return "\n".join(lines)
