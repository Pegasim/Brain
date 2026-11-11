"""Rüya günlüğü: giriş topla → ham kayıt → Whisper → Haiku değerlendirme işi.

Girişler (data/config/ruya.json):
  wa_jid         tek kişilik "🌙 Rüya" WhatsApp grubunun JID'i. Köprü store'u SALT-OKUR süzülür.
  sabah_dir      cfv-iphone'un sabah kestirmesi ham klasörü (varsayılan raw/sabah); yalnız govde.ruya SALT-OKUR okunur.
Ham: data/raw/ruya/wa/<YYYY-MM>/<msgId>.json (+ .ogg) — yalnız ekleme, var olan dosyaya dokunulmaz.
Türev: data/processed/ruya/{transkript,is}/. Kararlar: data/config/ruya.sqlite (silinmez, geri alma = undone_at).
Gece kuralı: D = girişin yerel (İstanbul) tarihi − 1 gün; metin "12 kas:" / "2026-11-12:" ile başlarsa o tarih.
GİZLİLİK: rüya metni hiçbir log/çıktı satırına girmez — loglara yalnız id ve uzunluk.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

from cfv import db
from cfv.timeutil import DEFAULT_TZ, UTC, iso

AGENT = "cfv-ruya-yorum"
WA_STORE = Path(os.environ.get("CFV_WA_STORE", "/srv/whatsapp/messages.jsonl"))
WHISPER_PY = Path(os.environ.get("CFV_WHISPER_PY", "/srv/cfv/ruya/.venv-whisper/bin/python"))
WHISPER_MODEL = "large-v3-turbo"
NO_RECALL = {"-", "yok", "hatırlamıyorum", "hatirlamiyorum", "hatırlamadım", "🌑", "x"}
WAIT_CLOSE = dt.timedelta(hours=4)    # gün kapanmazsa bu kadar sonra eldekiyle yorumla
MONTHS = ["oca", "şub", "mar", "nis", "may", "haz", "tem", "ağu", "eyl", "eki", "kas", "ara"]
PREFIX = re.compile(r"^\s*(?:(\d{4})-(\d{2})-(\d{2})|(\d{1,2})\s*([a-zçğıöşü]{3})[a-zçğıöşü]*)\s*[:\-–]\s*", re.I)


def base() -> Path: return db.data_dir() / "processed" / "ruya"
def raw_dir() -> Path: return db.data_dir() / "raw" / "ruya"
def jobs_dir() -> Path: return base() / "is"
def tr_dir() -> Path: return base() / "transkript"


def config() -> dict:
    p = db.data_dir() / "config" / "ruya.json"
    return json.loads(p.read_text()) if p.exists() else {}


def log(msg: str) -> None:
    p = db.data_dir() / "logs" / "ruya.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    line = f"{iso(dt.datetime.now(UTC))} {msg}"
    with p.open("a") as f:
        f.write(line + "\n")
    print(line)


def _parse_ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def _add_once(path: Path, data: bytes) -> bool:
    """Yalnız ekleme: dosya varsa dokunma. Yarım dosya kalmasın diye geçici ad + link."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    tmp.write_bytes(data)
    try:
        os.link(tmp, path)          # hedef varsa hata verir, asla üzerine yazmaz
    except FileExistsError:
        return False
    finally:
        tmp.unlink()
    return True


# ---- 1. topla -------------------------------------------------------------------------------
def collect_wa(store: Path | None = None) -> int:
    store, jid = store or WA_STORE, config().get("wa_jid")
    if not jid:
        return 0
    seen = {p.stem for p in raw_dir().glob("wa/*/*.json")}
    pos_file = base() / "okuma.json"          # artımlı okuma: dosya → {inode, offset}; kilit/yazma yok
    pos = json.loads(pos_file.read_text()) if pos_file.exists() else {}
    n = 0
    for f in sorted(store.parent.glob(store.name + "*")):   # ileride döndürme gelirse messages.jsonl.1 ...
        if f.suffix == ".gz":
            continue
        st, old = f.stat(), pos.get(f.name, {})
        off = old.get("offset", 0) if old.get("inode") == st.st_ino and old.get("offset", 0) <= st.st_size else 0
        with f.open("rb") as fh:
            fh.seek(off)
            chunk = fh.read()
        done = chunk.rfind(b"\n") + 1           # yarım yazılmış son satırı sonraki tura bırak
        pos[f.name] = {"inode": st.st_ino, "offset": off + done}
        for line in chunk[:done].decode("utf-8", "replace").splitlines():
            if jid not in line:
                continue
            r = json.loads(line)
            if r.get("chatJid") != jid or not r.get("fromMe") or r["id"] in seen:
                continue
            d = raw_dir() / "wa" / r["ts"][:7]
            src = Path(r["file"]) if r.get("file") else None
            if src and src.exists() and src.name.endswith(("-audio.ogg", "-audio.mp4")):
                _add_once(d / f"{r['id']}{src.suffix}", src.read_bytes())   # ses önce, kayıt sonra
            _add_once(d / f"{r['id']}.json", json.dumps(r, ensure_ascii=False).encode())
            seen.add(r["id"])
            n += 1
            log(f"topla wa {r['id']} uzunluk={len(r.get('text') or '')} ses={'evet' if src else 'hayır'}")
    pos_file.parent.mkdir(parents=True, exist_ok=True)
    pos_file.write_text(json.dumps(pos))
    return n


def parts() -> list[dict]:
    """Tüm ham girişler: {id, src, at, text, audio}. Ses parçasının metni transkriptten gelir."""
    out = []
    for f in raw_dir().glob("wa/*/*.json"):
        r = json.loads(f.read_text())
        audio = next((p for p in f.parent.glob(f"{f.stem}.*") if p.suffix != ".json"), None)
        ses = bool(audio) or (r.get("text") or "").startswith("[dosya]")   # dosyası inmemiş ses de "hatırladı"
        out.append({"id": r["id"], "src": "wa", "at": _parse_ts(r["ts"]), "text": None if ses else r.get("text"),
                    "audio": audio, "ses": ses})
    seen = set()           # cfv-iphone ham kaydı: raw/sabah/<YYYY-MM>/<gonderim_id>.json = {"alindi_at", "govde"}
    for f in sorted((db.data_dir() / "raw" / config().get("sabah_dir", "sabah")).glob("*/*.json")):
        try:
            g = json.loads(json.loads(f.read_text(encoding="utf-8"))["govde"])
        except (ValueError, KeyError, TypeError):
            continue
        r, gid = g.get("ruya"), g.get("gonderim_id") or f.stem
        if gid in seen or not isinstance(r, dict) or r.get("durum") not in ("gordum", "hatirlamiyorum"):
            continue                      # tekrar gönderim, "atla", "sorulmadi", alan yok: kayıt değil
        seen.add(gid)
        text = (r.get("metin") or "").strip() if r["durum"] == "gordum" else "hatırlamıyorum"
        at = r.get("cevaplandi_at") or r.get("soruldu_at")
        if text and at:                   # "gördüm" deyip boş bıraktıysa kayıt yok
            out.append({"id": "k-" + gid, "src": "kestirme", "at": _parse_ts(at), "text": text,
                        "audio": None, "ses": False})
    for p in out:
        t = tr_dir() / f"{p['id']}.txt"
        if p["audio"] and t.exists():
            p["text"] = t.read_text().strip()
    return sorted(out, key=lambda p: p["at"])


def night_of(at: dt.datetime, text: str | None) -> tuple[str, str | None]:
    """(gecenin tarihi D, öneki atılmış metin)."""
    local = at.astimezone(DEFAULT_TZ).date()
    m = PREFIX.match(text or "")
    if m:
        if m.group(1):
            d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        else:
            mon = m.group(5).lower().replace("i̇", "i")
            if mon not in MONTHS:
                return (local - dt.timedelta(days=1)).isoformat(), text
            d = dt.date(local.year, MONTHS.index(mon) + 1, int(m.group(4)))
            if d > local:
                d = d.replace(year=d.year - 1)
        return d.isoformat(), text[m.end():]
    return (local - dt.timedelta(days=1)).isoformat(), text


def is_no_recall(text: str | None) -> bool:
    return (text or "").strip().lower().rstrip(".!").strip() in NO_RECALL


def dreams() -> list[dict]:
    """Parçaları gecelere topla. recalled=0 yalnız tüm parçalar 'hatırlamıyorum' ise."""
    by: dict[str, dict] = {}
    for p in parts():
        day, text = night_of(p["at"], p["text"])
        d = by.setdefault(day, {"xid": f"ruya-{day}", "day": day, "first_at": p["at"], "parts": [], "texts": []})
        pending = p["audio"] is not None and p["text"] is None
        d["parts"].append({"id": p["id"], "src": p["src"], "at": iso(p["at"]),
                           "kind": "ses" if p["ses"] else "metin", "len": len(text or ""), "bekliyor": pending})
        if text and not is_no_recall(text):
            d["texts"].append(text.strip())
    for d in by.values():
        d["recalled"] = int(bool(d["texts"]) or any(x["kind"] == "ses" for x in d["parts"]))
        d["text"] = "\n\n".join(d["texts"]) or None
        d["ready"] = not any(x["bekliyor"] for x in d["parts"])
        d["imza"] = hashlib.sha1((d["text"] or "").encode()).hexdigest()
    return [by[k] for k in sorted(by)]


# ---- 2. Whisper (tek seferlik süreç; çıkınca VRAM serbest) -------------------------------------
_WHISPER = r"""
import json, sys, time
from faster_whisper import WhisperModel
m = WhisperModel(sys.argv[1], device="cuda", compute_type="int8_float16", download_root=sys.argv[2])
for src, dst in json.loads(sys.argv[3]):
    t = time.time()
    segs, info = m.transcribe(src, language="tr", vad_filter=True)
    open(dst, "w").write(" ".join(s.text.strip() for s in segs).strip() + "\n")
    print(json.dumps({"dst": dst, "sure": round(time.time() - t, 1), "ses_s": round(info.duration, 1)}))
"""


def transcribe() -> int:
    todo = [(str(p["audio"]), str(tr_dir() / f"{p['id']}.txt")) for p in parts()
            if p["audio"] and not (tr_dir() / f"{p['id']}.txt").exists()]
    if not todo:
        return 0
    tr_dir().mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "LD_LIBRARY_PATH": ":".join(
        glob.glob(str(WHISPER_PY.parent.parent / "lib/python3*/site-packages/nvidia/*/lib")))}
    res = subprocess.run(["nice", "-n", "19", "ionice", "-c3", str(WHISPER_PY), "-I", "-c", _WHISPER,
                          WHISPER_MODEL, str(WHISPER_PY.parent.parent.parent / "models"), json.dumps(todo)],
                         capture_output=True, text=True, env=env)
    for line in res.stdout.splitlines():
        r = json.loads(line)
        log(f"whisper {Path(r['dst']).stem} ses={r['ses_s']}s sure={r['sure']}s")
    if res.returncode:
        log(f"whisper HATA rc={res.returncode} {res.stderr.strip().splitlines()[-1:]}")
        alert("cfv-ruya: sesli rüya yazıya dökülemedi (rc %d)" % res.returncode)
    vram = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader"],
                          capture_output=True, text=True).stdout.strip()
    log(f"whisper bitti {len(todo)} dosya, VRAM şimdi: {vram}")
    return len(todo)


# ---- 3. bağlam + iş ---------------------------------------------------------------------------
def build_prompt(conn: sqlite3.Connection, d: dict) -> tuple[str, dict]:
    """Yalnız uyanıştan (first_at) önceki olaylar; D−2..D günleri. Döner: (istem, izinli ref'ler)."""
    until, day = iso(d["first_at"]), dt.date.fromisoformat(d["day"])
    d0 = (day - dt.timedelta(days=2)).isoformat()
    q = lambda sql, *a: conn.execute(sql, a).fetchall()
    w = (" AND e.local_date BETWEEN ? AND ? AND e.start_utc < ?", d0, d["day"], until)
    since90 = iso(d["first_at"] - dt.timedelta(days=90))
    people = q("""SELECT p.id, p.display_name, sum(e.start_utc >= ?) n90, count(*) n FROM event_person ep
                  JOIN event e ON e.id = ep.event_id JOIN person p ON p.id = ep.person_id
                  WHERE ep.role != 'dream' AND e.kind != 'dream' AND p.is_me = 0 AND e.start_utc < ?
                  GROUP BY p.id ORDER BY n90 DESC, n DESC LIMIT 60""", since90, until)
    refs: dict = {"P": {f"P{r[0]}" for r in people}, "E": set()}
    L = [f"# Rüya — {d['day']} gecesi", "", "## Rüya metni (Tuna'nın kendi anlatımı / sesli kaydın yazıya dökümü)",
         d["text"] or "", "", "## Tanınan kişiler (önem: son 90 gün / tüm zaman, yalnız geçmiş)",
         *(f"- P{r[0]} {r[1]} ({r[2]}/{r[3]})" for r in people), "",
         f"## Önceki günler ({d0} … {d['day']}, yalnız {until} öncesi)"]

    def ev(title, rows, fmt, cap):
        L.append(f"\n### {title}")
        for r in rows[:cap]:
            refs["E"].add(f"#{r[0]}")
            L.append(fmt(r))
        if len(rows) > cap:
            L.append(f"(+{len(rows) - cap} daha)")
    ev("Takvim", q("SELECT e.id, e.local_date, substr(e.start_utc,12,5), e.title FROM event e WHERE e.kind='calendar'" + w[0], *w[1:]),
       lambda r: f"- #{r[0]} {r[1]} {r[2]}Z {r[3]}", 60)
    ev("Gün raporları", q("SELECT e.id, e.local_date, substr(e.body,1,1500) FROM event e WHERE (e.kind='day_report' OR "
                          "(e.kind='report' AND json_extract(e.payload,'$.level') IN ('day','gün')))" + w[0], *w[1:]),
       lambda r: f"- #{r[0]} {r[1]}:\n{r[2]}", 3)
    ev("Notlar / ödevler", q("SELECT e.id, e.local_date, substr(e.title,1,100) FROM event e WHERE e.kind IN ('note','todo')" + w[0], *w[1:]),
       lambda r: f"- #{r[0]} {r[1]} {r[2]}", 40)
    ev("E-posta konuları", q("SELECT e.id, e.local_date, substr(e.title,1,100) FROM event e WHERE e.kind='email'" + w[0], *w[1:]),
       lambda r: f"- #{r[0]} {r[1]} {r[2]}", 30)
    ev("WhatsApp (sohbet × gün; Tuna'nın kendi mesajlarından kesit)", q(
        "SELECT min(e.id), e.local_date, e.title, count(*), group_concat(CASE WHEN json_extract(e.payload,'$.from_me') "
        "THEN substr(replace(e.body, char(10), ' '),1,100) END, ' | ') FROM event e WHERE e.kind='whatsapp'" + w[0]
        + " GROUP BY 2, 3 ORDER BY 4 DESC", *w[1:]),
       lambda r: f"- #{r[0]} {r[1]} {r[2]} ({r[3]} mesaj): {(r[4] or '')[:400]}", 40)
    ev("Fotoğraf/video (gün başına)", q(
        "SELECT min(e.id), e.local_date, count(*), (SELECT group_concat(n, ', ') FROM (SELECT DISTINCT p.display_name n "
        "FROM event x JOIN event_person ep ON ep.event_id=x.id JOIN person p ON p.id=ep.person_id WHERE x.local_date="
        "e.local_date AND x.kind IN ('photo','video') AND x.start_utc < ? LIMIT 15)) FROM event e WHERE e.kind IN "
        "('photo','video')" + w[0] + " GROUP BY 2", until, *w[1:]),
       lambda r: f"- #{r[0]} {r[1]}: {r[2]} kare; kişiler: {r[3] or '-'}", 3)
    # id'ler her gece ingest'inde değişir → kararlı anahtarlar meta'ya: kişi (source_kind, alias), olay (kaynak, external_id)
    stable = {"P": {}, "E": {}}
    for k in refs["P"]:
        a = conn.execute("SELECT source_kind, alias FROM person_alias WHERE person_id=? ORDER BY 1, 2 LIMIT 1",
                         (int(k[1:]),)).fetchone()
        if a:
            stable["P"][k] = list(a)
    for k in refs["E"]:
        r = conn.execute("SELECT s.kind, e.external_id, e.kind, e.local_date, substr(e.title,1,80) FROM event e "
                         "JOIN source s ON s.id = e.source_id WHERE e.id=?", (int(k[1:]),)).fetchone()
        stable["E"][k] = dict(zip(("source", "external_id", "kind", "date", "title"), r))
    return "\n".join(L)[:120_000] + "\n", stable


def _closed(day: str) -> tuple[bool, dict]:
    p = db.data_dir() / "processed" / "akis.sqlite"
    if not p.exists():
        return False, {}
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        d0 = (dt.date.fromisoformat(day) - dt.timedelta(days=2)).isoformat()
        revs = dict(c.execute("SELECT tarih, revizyon FROM gun WHERE tarih BETWEEN ? AND ?", (d0, day)))
    except sqlite3.Error:
        return False, {}
    return day in revs, revs


def job_key(d: dict) -> str:
    return f"{d['day']}-{d['imza'][:8]}"


def make_jobs(conn: sqlite3.Connection, now: dt.datetime | None = None, again: str | None = None) -> list[str]:
    now = now or dt.datetime.now(UTC)
    jobs_dir().mkdir(parents=True, exist_ok=True)
    made = []
    for d in dreams():
        if not d["recalled"] or not d["ready"]:
            continue
        key = job_key(d)
        if again == d["day"]:
            key += f"-r{len(list(jobs_dir().glob(key + '*.in.md'))) + 1}"
        elif list(jobs_dir().glob(key + "*.in.md")):
            continue
        closed, revs = _closed(d["day"])
        if not closed and now - d["first_at"] < WAIT_CLOSE and again != d["day"]:
            continue
        prompt, refs = build_prompt(conn, d)
        meta = {"key": key, "xid": d["xid"], "imza": d["imza"], "context_until": iso(d["first_at"]), "partial": int(not closed),
                "revizyonlar": revs, "refs": refs, "olusturuldu": iso(now)}
        (jobs_dir() / f"{key}.meta.json").write_text(json.dumps(meta, ensure_ascii=False))
        (jobs_dir() / f"{key}.in.md").write_text(prompt)
        made.append(key)
        log(f"iş {key} istem={len(prompt)} karakter kısmi={meta['partial']}")
    return made


def dispatch(keys: list[str]) -> None:
    if not keys:
        return
    shutil.copy(Path(__file__).with_name("ruya_agent.md"), jobs_dir() / "CLAUDE.md")
    st = subprocess.run(["bp", "status", "--json"], capture_output=True, text=True).stdout or "{}"
    if not any(a["name"] == AGENT and a["status"] != "closed" for a in json.loads(st).get("agents", [])):
        subprocess.run(["bp", "open", AGENT, str(jobs_dir()), "--claude", "--parent", "server-cfv", "--no-prompt", "--",
                        "--model", "claude-haiku-5-5", "--effort", "medium", "--strict-mcp-config",
                        "--disallowedTools", "Bash", "WebFetch", "WebSearch"], capture_output=True)
    for k in keys:
        subprocess.run(["bp", "msg", AGENT, f"iş: {k}.in.md -> {k}.json"], capture_output=True)
        log(f"gönderildi {k} -> {AGENT}")


def alert(msg: str) -> None:
    """bp uyarısı. Testte (pytest) ya da CFV_RUYA_UYARI=0 iken gönderilmez; gerçek veri dışında
    (CFV_DATA başka yer) gönderilirse başında DENEME ve tam log yolu olur."""
    logp = db.data_dir().resolve() / "logs" / "ruya.log"
    log("UYARI " + msg)
    if os.environ.get("CFV_RUYA_UYARI", "1") == "0" or "PYTEST_CURRENT_TEST" in os.environ:
        return
    real = db.data_dir().resolve() == (db.REPO_ROOT / "data").resolve()
    subprocess.run(["bp", "msg", "server-cfv", f"{'' if real else 'DENEME '}{msg} (log: {logp})"], capture_output=True)


# ---- 4. kararlar (kişi önerisi onay/ret; (source_kind, alias) anahtarlı, silinmez) --------------
def decisions_db() -> sqlite3.Connection:
    p = db.data_dir() / "config" / "ruya.sqlite"
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE IF NOT EXISTS karar (id INTEGER PRIMARY KEY, at TEXT NOT NULL, xid TEXT NOT NULL,
                 p_kind TEXT NOT NULL, p_alias TEXT NOT NULL, verdict TEXT NOT NULL CHECK (verdict IN ('evet','hayir')),
                 undone_at TEXT)""")
    return c


def effective_decisions() -> dict[tuple[str, str, str], str]:
    with decisions_db() as c:
        rows = c.execute("SELECT xid, p_kind, p_alias, verdict FROM karar WHERE undone_at IS NULL ORDER BY id")
        return {(x, k, a): v for x, k, a, v in rows}


# ---- tur (systemd) -----------------------------------------------------------------------------
def run() -> None:
    collect_wa()
    transcribe()
    conn = db.connect()
    with (db.data_dir() / "processed" / ".akis.lock").open("a") as lock:
        try:                                  # gece turu sürüyorsa ingest'i o yapar (ingest all dream'i içerir)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            from cfv.sources import dream
            conn.executescript(db.schema_sql())
            with conn:
                dream.ingest(conn)
        except BlockingIOError:
            log("akis turu sürüyor, ingest bu tur atlandı")
    dispatch(make_jobs(conn))


# ---- komutlar: cfv ruya ... -------------------------------------------------------------------
def cli(argv: list[str]) -> None:
    cmd, *a = argv or ["durum"]
    if cmd == "tur":
        run()
    elif cmd == "durum":                     # metin YOK: tarih, parça, uzunluk, değerlendirme
        conn = db.connect()
        ev = dict(conn.execute("SELECT xid, emotion FROM dream_eval"))
        for d in dreams()[-int(a[0] if a else 14):]:
            print(f"{d['day']}  {'rüya' if d['recalled'] else 'yok '}  parça={len(d['parts'])} "
                  f"uzunluk={len(d['text'] or '')}  {'yorum: ' + str(ev[d['xid']]) if d['xid'] in ev else 'yorum yok'}"
                  f"{'' if d['ready'] else '  (ses bekliyor)'}")
    elif cmd == "goster":                    # Tuna'nın terminali için: metin + yorum
        conn = db.connect()
        r = conn.execute("SELECT d.text, e.raw_json FROM dream d LEFT JOIN dream_eval e USING (xid) WHERE d.local_date=?",
                         (a[0],)).fetchone()
        print("(kayıt yok)" if not r else f"{r[0] or '(hatırlamıyorum)'}\n\n"
              + (json.dumps(json.loads(r[1]), ensure_ascii=False, indent=1) if r[1] else "(yorum yok)"))
    elif cmd == "karar":                     # cfv ruya karar <tarih> <P-id|ad> evet|hayir
        day, who, verdict = a
        conn = db.connect()
        pid = int(who[1:]) if re.fullmatch(r"P\d+", who) else None
        if pid is None:
            hits = conn.execute("SELECT id FROM person WHERE display_name LIKE ?", (f"%{who}%",)).fetchall()
            if len(hits) != 1:
                raise SystemExit(f"{len(hits)} kişi eşleşti; P<id> ver")
            pid = hits[0][0]
        k = conn.execute("SELECT source_kind, alias FROM person_alias WHERE person_id=? ORDER BY 1, 2 LIMIT 1",
                         (pid,)).fetchone()
        with decisions_db() as c:
            i = c.execute("INSERT INTO karar (at, xid, p_kind, p_alias, verdict) VALUES (?,?,?,?,?)",
                          (iso(dt.datetime.now(UTC)), f"ruya-{day}", *k, verdict)).lastrowid
        print(f"karar {i}: ruya-{day} P{pid} {verdict} (geri almak: cfv ruya geri {i}); etkisi sonraki ingest'te")
    elif cmd == "geri":
        with decisions_db() as c:
            c.execute("UPDATE karar SET undone_at=? WHERE id=? AND undone_at IS NULL", (iso(dt.datetime.now(UTC)), int(a[0])))
        print(f"karar {a[0]} geri alındı")
    elif cmd == "yeniden":                   # cfv ruya yeniden <tarih>: aynı metinle yeni yorum (eskisi kalır)
        dispatch(make_jobs(db.connect(), again=a[0]))
    else:
        raise SystemExit("cfv ruya tur|durum [N]|goster <tarih>|karar <tarih> <kişi> evet|hayir|geri <id>|yeniden <tarih>")
