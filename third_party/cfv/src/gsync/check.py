#!/usr/bin/env python3
"""Google Takvim + Gmail senkronunun günlük sağlık kontrolü (yalnız okur).

Bakılanlar (data/raw/google-calendar/, data/raw/gmail/<hesap>/ state.json + veri dosyası):
  takvim: last_ok < 26 sa, failures = 0, events.json fetched_at < 26 sa,
          takvim sayısı önceki kontrolden az değil, olay sayısı önceki kontrolün %95'inden az değil
  gmail (data/config/gmail-accounts): last_fetch < 26 sa, failures = 0, total azalmamış
Her gün data/logs/gsync-check.jsonl'a bir satır. Sorun varsa `bp msg server-cfv`;
(systemd'den "[service:cfv-gsync-check]" imzasıyla, BP_OWNER=server-cfv).
bp gönderemezse journal'a `logger` + laptop-notify;
3 kontrol üst üste sorunluysa her durumda laptop-notify. Sorun yoksa sessiz.
Kullanım: check.py [--dry-run | --test-alert]
  --dry-run:    yazmaz, mesaj atmaz, sonucu basar
  --test-alert: veriye/log'a dokunmadan uyarı yolunu "DENEME" mesajıyla dener (bp → yedek yol)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

DATA = Path(os.environ.get("CFV_DATA", Path(__file__).resolve().parents[2] / "data"))
MAX_AGE = dt.timedelta(hours=26)
MIN_EVENT_RATIO = 0.95
NOTIFY_AFTER = 3


def _t(s: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def _load(p: Path, default=None):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def last_record(log: Path) -> dict:
    try:
        lines = log.read_text().splitlines()
    except FileNotFoundError:
        return {}
    return json.loads(lines[-1]) if lines else {}


def check(now: dt.datetime, prev: dict) -> dict:
    problems: list[str] = []

    def old(t: dt.datetime | None) -> bool:
        return t is None or now - t > MAX_AGE

    g = DATA / "raw" / "google-calendar"
    st = _load(g / "state.json", {})
    ev = _load(g / "events.json")
    cal = {"last_ok": st.get("last_ok"), "failures": st.get("failures"), "last_error": st.get("last_error")}
    if ev is None:
        problems.append("takvim: events.json yok/okunamadı")
    else:
        cal.update(fetched_at=ev.get("fetched_at"), calendars=len(ev.get("calendars") or []),
                   events=len(ev.get("events") or []))
    if old(_t(st.get("last_ok"))):
        problems.append(f"takvim: son başarılı çekim {st.get('last_ok')} (>26 sa)")
    if st.get("failures"):
        problems.append(f"takvim: {st['failures']} hata üst üste: {str(st.get('last_error'))[:160]}")
    pc = prev.get("gcal") or {}
    if "calendars" in cal:
        if cal["calendars"] == 0 or cal["calendars"] < pc.get("calendars", 0):
            problems.append(f"takvim: takvim sayısı {pc.get('calendars')} → {cal['calendars']}")
        if pc.get("events") and cal["events"] < MIN_EVENT_RATIO * pc["events"]:
            problems.append(f"takvim: olay sayısı {pc['events']} → {cal['events']}")
    # events.json yalnız değişince yenilenir; tazelik last_ok'tan bakılır

    acc_file = DATA / "config" / "gmail-accounts"
    accounts = [a.strip() for a in acc_file.read_text().splitlines() if a.strip()] if acc_file.exists() else []
    if not accounts:
        problems.append("gmail: data/config/gmail-accounts boş/yok")
    gm = {}
    for i, a in enumerate(accounts, 1):
        s = _load(DATA / "raw" / "gmail" / a / "state.json", {})
        gm[a] = {"last_fetch": s.get("last_fetch"), "failures": s.get("failures"), "total": s.get("total")}
        tag = f"gmail hesap {i}"  # mesajda hesap adı yazmaz
        if old(_t(s.get("last_fetch"))):
            problems.append(f"{tag}: son çekim {s.get('last_fetch')} (>26 sa)")
        if s.get("failures"):
            problems.append(f"{tag}: {s['failures']} hata üst üste")
        pt = (prev.get("gmail") or {}).get(a, {}).get("total")
        if pt is not None and (s.get("total") or 0) < pt:
            problems.append(f"{tag}: mesaj sayısı {pt} → {s.get('total')}")

    streak = (prev.get("streak", 0) + 1) if problems else 0
    return {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "ok": not problems, "problems": problems,
            "streak": streak, "gcal": cal, "gmail": gm}


def alert(msg: str, streak: int) -> bool:
    """bp msg server-cfv; olmazsa journal + laptop. bp gönderdiyse True."""
    sent = subprocess.run(["bp", "msg", "server-cfv", msg[:1500]], check=False,
                          capture_output=True).returncode == 0
    if not sent:  # bp gönderemezse (eski bp / kimlik yok) → journal + laptop
        subprocess.run(["logger", "-t", "cfv-gsync-check", msg[:1500]], check=False)
    if not sent or streak >= NOTIFY_AFTER:
        subprocess.run(["laptop-notify", "cfv senkron bozuk", f"{streak} gündür sorun: {msg[:300]}",
                        "normal"], check=False)
    return sent


def main() -> int:
    if "--test-alert" in sys.argv:
        sent = alert("DENEME (cfv-gsync-check uyarı yolu sınaması, gerçek sorun yok)", 0)
        print("bp gönderdi" if sent else "bp gönderemedi → logger + laptop-notify")
        return 0 if sent else 1
    dry = "--dry-run" in sys.argv
    log = DATA / "logs" / "gsync-check.jsonl"
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    r = check(now, last_record(log))
    if dry:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0
    os.umask(0o077)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as f:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
    if r["problems"]:
        msg = "cfv gsync-check SORUN: " + " | ".join(r["problems"]) + f" (ayrıntı: {log})"
        alert(msg, r["streak"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
