"""Gece konsolidasyonu (cfv-gunluk.service): ham veri → cfv.sqlite → timeline → foto türevleri.

    python -m akis.gunluk [--dry-run] [--no-wait] [--skip-foto]   gece turu
    python -m akis.gunluk match                                   haftalık derive.py match (cfv-derive-match)
    python -m akis.gunluk --test-alert                            uyarı yolunu "DENEME" mesajıyla dene

Sıra: çekimler bitene kadar bekle → cfv.sqlite tarihli zstd yedeği → sahne kopyada `cfv ingest all` → kaynak başına
azalma denetimi (DUR: canlı DB'ye yazmaz, bp msg) → SQLite backup API ile canlıya → timeline yedeği + `cfv tree` →
gün kapanışı (akis.sqlite) → foto türevleri (değişen dosyalar önce yedeklenir) → buyume.tsv → tazelik denetimi.
Otomatik silme yok: yedekler birikir, eşikte haber verilir. Toplu işler nice 19 / ionice -c3 / en çok 4 işçi.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from cfv import db

from akis import buyume, gun, sayim, tazelik

REPO = Path(__file__).resolve().parents[2]
NICE = ["nice", "-n", "19", "ionice", "-c3"]
UV = shutil.which("uv") or "/root/.local/bin/uv"
# Gece turu çekimlerin BİTİŞİNE bağlı: timer 05:30'da tetikler, burada her gerekli çekimin bugün (05:00'ten sonra)
# başlayıp bitmesi beklenir. Timer'lı işlerde After= yetmez; durum systemctl'den okunur. Cmt foto tam tur uzun sürebilir.
REQUIRED = {"foto": ["cfv-icloudpd@gunluk.service", "cfv-icloudpd@tam.service"],  # hangisi bugün koştuysa
            "gmail": ["cfv-gmail-sync.service"]}
OPTIONAL = ["cfv-gcal-sync.service", "cfv-foto-ikinci.service"]                  # yalnız çalışıyorsa beklenir
DAY_START_H = 5
# borg (server-cfv, 2026-11-10): data/ her gece 03:30'da yedekleniyor → 03:30–04:00 arası cfv.sqlite'a yazma yok.
# Payla: 03:25–04:05. Elle tur bu pencereye denk gelirse canlıya yazmadan önce bekler.
BORG_WINDOW = ((3, 25), (4, 5))
WAIT_MAX_S = 2 * 3600
# azalma eşiği: max(ABS, oran × önceki); takvimden silinen olay olağan → calendar-google için %5
DROP_ABS, DROP_RATIO = 10, 0.01
DROP_RATIO_KIND = {"calendar-google": 0.05}
KIND_OF = {"photos": "iphone-photos", "notes": "apple-notes", "reports": "reports"}  # cfv ingest adı → source.kind
# foto türevleri: tur boyunca değişebilecek dosyalar (değiştiyse önceki hali yedek-gunluk/<tarih>/'e)
FOTO_FILES = ["signals.json", "scores.tsv", "stickers.json", "hash256.tsv", "mid/yollar.tsv", "vectors.sqlite"]
VECTORS_EVERY = dt.timedelta(days=7)  # vectors.sqlite (~50 MB zst, yeniden üretilebilir) en çok haftada bir


def proc() -> Path:
    return db.data_dir() / "processed"


def logs() -> Path:
    return db.data_dir() / "logs"


def log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    logs().mkdir(parents=True, exist_ok=True)
    with open(logs() / "gunluk.log", "a") as f:
        f.write(line + "\n")


def run(cmd: list[str], *, timeout: int = 4 * 3600, env: dict | None = None) -> tuple[int, str, float]:
    t = time.time()
    e = {**os.environ, "CFV_WORKERS": "4", **(env or {})}
    try:
        p = subprocess.run(NICE + cmd, cwd=REPO, env=e, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout + p.stderr).strip()
        rc = p.returncode
    except subprocess.TimeoutExpired as ex:
        out, rc = f"ZAMAN AŞIMI ({timeout} sn): {ex}", 124
    return rc, out, time.time() - t


def lock():
    p = proc() / ".akis.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    f = open(p, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("başka bir akış turu çalışıyor; çıkıyorum")
        sys.exit(0)
    return f


def unit_states(units: list[str]) -> dict[str, dict]:
    """birim → {state, result, start (unix|None)}"""
    try:
        out = subprocess.run(["systemctl", "show", "--timestamp=unix", "-p",
                              "Id,ActiveState,Result,InactiveExitTimestamp", *units],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    res = {}
    for block in out.strip().split("\n\n"):
        kv = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        ts = kv.get("InactiveExitTimestamp", "").lstrip("@")
        res[kv.get("Id", "?")] = {"state": kv.get("ActiveState"), "result": kv.get("Result"),
                                  "start": float(ts) if ts else None}
    return res


def pending_fetchers(since: float) -> list[str]:
    """Henüz bitmemiş çekimler: gerekli olan bugün başlamadıysa ya da sürüyorsa; isteğe bağlı olan sürüyorsa."""
    st = unit_states([u for us in REQUIRED.values() for u in us] + OPTIONAL)
    if not st:
        return []  # systemctl yok (elle/test) → bekleme
    busy = lambda u: st.get(u, {}).get("state") in ("active", "activating", "deactivating", "reloading")  # noqa: E731
    out = []
    for name, units in REQUIRED.items():
        run_ = [u for u in units if busy(u)]
        if run_:
            out.append(f"{name}: {', '.join(run_)} sürüyor")
        elif not any((st.get(u, {}).get("start") or 0) >= since for u in units):
            out.append(f"{name}: bugün henüz başlamadı ({', '.join(units)})")
    out += [f"{u} sürüyor" for u in OPTIONAL if busy(u)]
    return out


def wait_fetchers(now: dt.datetime | None = None) -> list[str]:
    """Çekimler bitene kadar bekle (en çok WAIT_MAX_S). Döner: süre dolduğunda hâlâ eksik olanlar."""
    now = now or dt.datetime.now().astimezone()
    start = now.replace(hour=DAY_START_H, minute=0, second=0, microsecond=0)
    if start > now:
        start -= dt.timedelta(days=1)
    since = start.timestamp()
    t0 = time.time()
    pend = pending_fetchers(since)
    if pend:
        log(f"çekimler bekleniyor: {'; '.join(pend)}")
    while pend and time.time() - t0 < WAIT_MAX_S:
        time.sleep(60)
        pend = pending_fetchers(since)
    if pend:
        log(f"eksik: {'; '.join(pend)} — {WAIT_MAX_S // 3600} sa beklendi, eldekiyle devam (kalan ertesi tur)")
    elif time.time() - t0 > 5:
        log(f"çekimler bitti ({(time.time() - t0) / 60:.0f} dk beklendi)")
    st = unit_states([u for us in REQUIRED.values() for u in us])
    failed = [u for u, v in st.items() if v.get("result") not in (None, "success") and (v.get("start") or 0) >= since]
    if failed:
        log(f"uyarı: bugünkü çekim başarısız bitti: {', '.join(failed)} (eldekiyle devam)")
    return pend


def borg_wait(now: dt.datetime | None = None) -> float:
    """Borg penceresindeysek bitene kadar bekle; beklenen saniyeyi döner."""
    now = now or dt.datetime.now()
    (h0, m0), (h1, m1) = BORG_WINDOW
    a, b = now.replace(hour=h0, minute=m0, second=0, microsecond=0), now.replace(hour=h1, minute=m1, second=0,
                                                                              microsecond=0)
    if not (a <= now < b):
        return 0.0
    wait = (b - now).total_seconds()
    log(f"borg penceresi ({h0:02d}:{m0:02d}–{h1:02d}:{m1:02d}): canlıya yazmadan önce {wait / 60:.0f} dk bekliyorum")
    time.sleep(wait)
    return wait


def sqlite_copy(src: Path, dst: Path) -> None:
    """Tutarlı kopya (SQLite online backup API): canlı DB ya da canlıya — okuyucular bozulmaz."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def drops(before: dict[str, int], after: dict[str, int], skipped: set[str]) -> list[str]:
    out = []
    for k, b in sorted(before.items()):
        a = after.get(k, 0)
        lim = max(DROP_ABS, math.ceil(b * DROP_RATIO_KIND.get(k, DROP_RATIO)))
        if b - a > lim:
            out.append(f"{k}: {b} → {a} ({a - b:+d}, eşik -{lim})")
    for name in sorted(skipped):
        out.append(f"{name}: ham veri bulunamadı, ATLANDI")
    return out


def rm_sqlite(p: Path) -> None:
    """Bu turun kendi geçici sahne kopyası (canlı DB'nin aynısı) — ham/türetilmiş veri değil."""
    for q in (p, p.with_name(p.name + "-wal"), p.with_name(p.name + "-shm")):
        q.unlink(missing_ok=True)


# --- foto türevleri yedeği -------------------------------------------------------------------------------

def _stamp(p: Path):
    try:
        s = p.stat()
        return s.st_size, s.st_mtime_ns
    except FileNotFoundError:
        return None


def foto_pre(today: str) -> dict:
    out = proc() / "photos"
    yd = out / "yedek-gunluk"
    tmp = yd / ".tur"
    tmp.mkdir(parents=True, exist_ok=True)
    state = tazelik._json(yd / "son.json")
    taken = {}
    for rel in FOTO_FILES:
        p = out / rel
        st = _stamp(p)
        if st is None:
            continue
        if rel == "vectors.sqlite":
            last = state.get(rel, {}).get("tarih")
            if last and dt.date.fromisoformat(today) - dt.date.fromisoformat(last) < VECTORS_EVERY:
                continue
        z = tmp / (rel.replace("/", "-") + ".zst")
        z.unlink(missing_ok=True)
        subprocess.run(NICE + ["zstd", "-q", "-T2", "-o", str(z), str(p)], check=True)
        taken[rel] = (st, z)
    return taken


def foto_post(taken: dict, today: str) -> list[str]:
    out = proc() / "photos"
    yd = out / "yedek-gunluk"
    state = tazelik._json(yd / "son.json")
    saved = []
    for rel, (st, z) in taken.items():
        if _stamp(out / rel) != st:
            dst = yd / today / z.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():  # aynı gün ikinci tur: ilk yedek kalır
                z.unlink()
                continue
            z.rename(dst)
            state[rel] = {"tarih": today, "boyut": st[0]}
            saved.append(rel)
        else:
            z.unlink()  # değişmedi: geçici sıkıştırılmış kopya
    (yd / "son.json").write_text(json.dumps(state, ensure_ascii=False, indent=1))
    return saved


def ensure_photos_bin() -> str | None:
    """bin/ git dışı: koşulan ağaçta (prod worktree) ./bin/photos yoksa ya da src/photos/*.go'dan eskiyse derle.
    Hata metni döner (None = hazır)."""
    binp = REPO / "bin" / "photos"
    srcs = list((REPO / "src" / "photos").glob("*.go"))
    if binp.exists() and all(binp.stat().st_mtime >= f.stat().st_mtime for f in srcs):
        return None
    rc, out, s = run(["go", "build", "-o", str(binp), "./src/photos"], timeout=1800,
                     env={"HOME": os.environ.get("HOME", "/root")})  # go.mod/go.sum salt-okur (-mod=readonly varsayılan)
    log(f"[foto] bin/photos derlendi rc={rc} {s:.0f} sn {out[-300:] if rc else ''}")
    return None if rc == 0 else f"bin/photos derlenemedi rc={rc}: {out[-200:]}"


def foto_steps(new_iphone: bool) -> tuple[list[dict], list[str]]:
    steps = [("embed", [UV, "run", "--quiet", "src/photos/embed.py", "embed"]),
             ("mid", [UV, "run", "--quiet", "src/photos/derive.py", "mid"]),
             ("signals", ["./bin/photos", "signals"]),
             ("score", ["./bin/photos", "score"])]
    if new_iphone:
        steps.insert(1, ("stickers", [UV, "run", "--quiet", "src/photos/embed.py", "stickers"]))
    res, problems = [], []
    err = ensure_photos_bin()
    if err:
        problems.append(err)
        steps = [x for x in steps if x[1][0] != "./bin/photos"]
    for name, cmd in steps:
        rc, out, s = run(cmd)
        tail = out.splitlines()[-1] if out else ""
        log(f"[foto:{name}] rc={rc} {s:.0f} sn  {tail[:300]}")
        res.append({"adim": name, "rc": rc, "sn": round(s)})
        if rc != 0:
            problems.append(f"foto adımı {name} rc={rc}: {tail[:200]}")
            log(out[-2000:])
        if name in ("embed", "stickers"):
            vram = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader"],
                                  capture_output=True, text=True).stdout.strip()
            log(f"[foto:{name}] GPU bellek (süreç bitti): {vram}")
    return res, problems


# --- tur ---------------------------------------------------------------------------------------------------

def nightly(args) -> int:
    lk = lock()  # noqa: F841 (süreç boyunca açık kalır)
    t0 = time.time()
    today = dt.date.today().isoformat()
    live = db.default_db_path()
    summary: dict = {"tarih": today, "basla": dt.datetime.now().isoformat(timespec="seconds"),
                     "dry_run": args.dry_run, "sureler": {}}
    run_problems: list[str] = []
    log(f"=== gece turu başladı{' (DENEME: canlıya yazmaz)' if args.dry_run else ''}")

    summary["eksik"] = [] if args.no_wait else wait_fetchers()
    before = sayim.counts(live)
    alias_before = sayim.alias_count(live)
    ev_before = sayim.eval_match(live)
    src_before = {k: snap for k, _, snap, _ in sayim.sources(live)}

    # 1) sahne kopyası + tarihli yedek
    stage_dir = proc() / ".gunluk"
    stage_dir.mkdir(exist_ok=True)
    stage = stage_dir / (f"cfv-{today}-deneme.sqlite" if args.dry_run else f"cfv-{today}.sqlite")
    if stage.exists():  # önceki DUR'un incelenecek kopyası: üzerine yazma, kenara al
        keep = stage.with_name(stage.stem + f".dur-{int(stage.stat().st_mtime)}.sqlite")
        stage.rename(keep)
        log(f"önceki sahne kopyası kenara alındı: {keep.name}")
    rm_sqlite(stage)
    t = time.time()
    sqlite_copy(live, stage)
    summary["sureler"]["kopya"] = round(time.time() - t)
    if not args.dry_run:
        yd = proc() / "yedek"
        yd.mkdir(exist_ok=True)
        bk = yd / f"cfv-{today}-gunluk.sqlite.zst"
        if bk.exists():
            log(f"bugünün yedeği zaten var: {bk.name}")
        else:
            t = time.time()
            subprocess.run(NICE + ["zstd", "-q", "-T2", "-o", str(bk), str(stage)], check=True)
            summary["sureler"]["yedek"] = round(time.time() - t)
            log(f"yedek: {bk.name} {bk.stat().st_size / 2**20:.0f} MB")
        summary["yedek_bayt"] = bk.stat().st_size

    # 2) sahnede ingest
    rc, out, s = run([sys.executable, "-m", "cfv.cli", "--db", str(stage), "ingest", "all"], timeout=3 * 3600)
    summary["sureler"]["ingest"] = round(s)
    for line in out.splitlines():
        log(f"  {line[:400]}")
    skipped = set(re.findall(r"^\[([\w-]+)\] ATLANDI", out, re.M))
    after = sayim.counts(stage) if rc == 0 else {}
    if rc != 0:
        problems = [f"ingest rc={rc}: {out.splitlines()[-1][:300] if out else ''}"]
    else:
        # ATLANDI (ham veri yok): o tür önceden olay taşıyorsa DUR; hiç verisi olmamış kaynak (ör. ics) sorun değil
        problems = drops(before, after, {n for n in skipped if before.get(KIND_OF.get(n, n), 0) > 0})
        alias_after = sayim.alias_count(stage)
        if alias_after < alias_before:  # takma adlar hiç silinmez: her düşüş beklenmedik
            problems.append(f"person_alias {alias_before} → {alias_after} ({alias_after - alias_before:+d})")
        summary["person_alias"] = f"{alias_before}→{alias_after}"
        ev_after = sayim.eval_match(stage)
        if ev_after[1] < ev_before[1]:
            problems.append(f"eval UUID eşleşmesi {ev_before[1]}/{ev_before[0]} → {ev_after[1]}/{ev_after[0]}")
        summary["eval"] = f"{ev_after[1]}/{ev_after[0]}"
    summary["once"], summary["sonra"] = before, after
    diff = {k: after.get(k, 0) - before.get(k, 0) for k in sorted(set(before) | set(after))}
    log("önce→sonra: " + ", ".join(f"{k} {before.get(k, 0)}→{after.get(k, 0)} ({d:+d})" for k, d in diff.items()))
    if problems:
        msg = "cfv gece turu DUR (canlı DB'ye yazılmadı): " + "; ".join(problems) + \
              f". Sahne kopyası: data/processed/.gunluk/{stage.name}"
        log(msg)
        summary["durum"] = "DUR"
        summary["sorunlar"] = problems
        _summary(summary, t0)
        if args.dry_run:
            rm_sqlite(stage)
        else:
            tazelik.alert(msg)
        return 2
    if args.dry_run:
        rm_sqlite(stage)
        log("DENEME bitti: denetim geçti, canlı DB'ye dokunulmadı")
        summary["durum"] = "deneme-tamam"
        _summary(summary, t0)
        return 0

    # 3) canlıya yayın (borg penceresi dışında)
    borg_wait()
    t = time.time()
    with sqlite3.connect(stage) as c:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    sqlite_copy(stage, live)
    if sayim.counts(live) != after:
        raise RuntimeError("yayın sonrası canlı DB sayıları sahneyle aynı değil")
    rm_sqlite(stage)
    summary["sureler"]["yayin"] = round(time.time() - t)
    log("canlı cfv.sqlite güncellendi")

    # 4) timeline
    tl = proc() / "timeline"
    tb = proc() / "yedek" / f"timeline-{today}.tar.gz"
    t = time.time()
    if tl.exists() and not tb.exists():
        subprocess.run(NICE + ["tar", "-czf", str(tb), "-C", str(proc()), "timeline"], check=True)
    rc, out, s = run([sys.executable, "-m", "cfv.cli", "tree"])
    summary["sureler"]["tree"] = round(time.time() - t)
    log(f"[tree] rc={rc} {out.splitlines()[-1][:300] if out else ''}")
    if rc != 0:
        run_problems.append(f"cfv tree rc={rc}")
    broken = sayim.broken_symlinks(tl)
    summary["kirik_symlink"] = broken
    if broken:
        run_problems.append(f"timeline'da {broken} kırık symlink")
    summary["timeline_yedek_bayt"] = tb.stat().st_size if tb.exists() else 0

    # 5) gün kapanışı
    t = time.time()
    g = gun.close_days(live)
    summary["sureler"]["gun"] = round(time.time() - t)
    summary["gun"] = {"dun": g["dun"], "kapanan": len(g["kapanan"]), "revize": g["revize"][-50:]}
    log(f"[gün] dün={g['dun']} kapanan={len(g['kapanan'])} revize={len(g['revize'])} "
        f"{','.join(g['revize'][-10:])}")

    # 6) foto türevleri
    if not args.skip_foto:
        src_after = {k: snap for k, _, snap, _ in sayim.sources(live)}
        new_iphone = src_after.get("whatsapp") != src_before.get("whatsapp")
        t = time.time()
        taken = foto_pre(today)
        res, fp = foto_steps(new_iphone)
        saved = foto_post(taken, today)
        summary["sureler"]["foto"] = round(time.time() - t)
        summary["foto"] = res
        summary["foto_yedek"] = saved
        run_problems += fp
        log(f"[foto] yedeklenen (değişen): {', '.join(saved) or '-'}")

    # 7) büyüme + tazelik
    final = sayim.counts(live)
    line = buyume.append(final, sayim.photo_derivs())
    log(f"[büyüme] {line.strip() if line else 'bugünün satırı zaten var'}")
    ev = tazelik.evaluate(live)
    probs = ev["sorunlar"] + [(f"tur-{i}", p) for i, p in enumerate(run_problems)]
    send = tazelik.due(probs)
    summary["sorunlar"] = [p for _, p in probs]
    summary["durum"] = "tamam" if not probs else "tamam-sorunlu"
    _summary(summary, t0)
    log(f"=== gece turu bitti ({(time.time() - t0) / 60:.1f} dk), sorun: {len(probs)}")
    if send:
        tazelik.alert("cfv gece turu: " + "; ".join(send))
    return 0


def _summary(summary: dict, t0: float) -> None:
    summary["toplam_sn"] = round(time.time() - t0)
    with open(logs() / "gunluk.jsonl", "a") as f:
        f.write(json.dumps(summary, ensure_ascii=False, default=str) + "\n")


def weekly_match() -> int:
    lk = lock()  # noqa: F841
    out = proc() / "photos"
    derivs = out / "derivs.tsv"
    newest = 0
    try:
        newest = max(e.stat().st_mtime for e in os.scandir(out / "mid") if e.name.endswith(".jpg"))
    except ValueError:
        pass
    if derivs.exists() and derivs.stat().st_mtime >= newest:
        log("[match] derivs.tsv'den sonra yeni mid/ yok; atlandı")
        return 0
    log("[match] derive.py match başlıyor (derivs.tsv'yi kendisi derivs.<mtime>.tsv olarak yedekler)")
    rc, o, s = run([UV, "run", "--quiet", "src/photos/derive.py", "match"], timeout=6 * 3600)
    log(f"[match] rc={rc} {s / 60:.0f} dk  {o.splitlines()[-1][:300] if o else ''}")
    if rc != 0:
        tazelik.alert(f"cfv haftalık derive match rc={rc}: {o.splitlines()[-1][:300] if o else ''}")
        return 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="akis.gunluk")
    ap.add_argument("cmd", nargs="?", default="gece", choices=["gece", "match"])
    ap.add_argument("--dry-run", action="store_true", help="sahnede ingest + denetim; canlıya/yedeğe yazmaz")
    ap.add_argument("--no-wait", action="store_true", help="çekimlerin bitmesini bekleme")
    ap.add_argument("--skip-foto", action="store_true")
    ap.add_argument("--test-alert", action="store_true")
    a = ap.parse_args(argv)
    if a.test_alert:
        ok = tazelik.alert("DENEME — cfv-akis uyarı yolu sınanıyor, gerçek sorun yok")
        print("bp gönderdi" if ok else "bp gönderemedi → logger + laptop-notify")
        return 0
    if a.cmd == "match":
        return weekly_match()
    try:
        return nightly(a)
    except Exception as e:  # beklenmedik hata: sessiz kalmasın
        log(f"HATA: {type(e).__name__}: {e}")
        if not a.dry_run:
            tazelik.alert(f"cfv gece turu HATA: {type(e).__name__}: {str(e)[:300]}")
        raise


if __name__ == "__main__":
    sys.exit(main())
