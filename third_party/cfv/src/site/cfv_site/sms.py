"""⛔ KULLANILMIYOR (2026-11-09, Tuna kararı): SMS işi server-sms agent'ına geçti; bu modül sunucuya/servise
bağlı DEĞİL. Silinmedi (server-cfv: hiçbir şey silme); server-sms isterse referans olarak okuyabilir.

iPhone Kestirmeler → SMS ham kaydı (POST /ingest/sms, Bearer token).

- Token yalnız `src/site/cfv-site sms-token` ile Tuna'nın terminalinde üretilir ve yalnız ekrana basılır;
  sunucuda sha256'sı durur: <state>/sms-token (600). Dosya yoksa uç KAPALI (503).
- Kayıt: <CFV_SMS_DIR>/YYYY-MM.jsonl, yalnız ekleme (600). Satır: received_at, sender, text, alindi_utc.
  cfv.sqlite'a aktarma burada değil (server-cfv okuyucusu).
- Tekilleştirme: sha256(sender + normalize metin); aynı anahtar son yazılışından 10 dk içinde gelirse yazılmaz
  (200 döner). Son saatin anahtarları bellekte; açılıştan sonraki ilk istekte dosyadan yüklenir.
- Tek kullanımlık kod (OTP) içeren SMS hiç yazılmaz; yalnız aylık sayaç: <state>/sms-otp-count.json.
- Metin/gövde loglanmaz.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
from pathlib import Path

from cfv_site.auth import state_dir

DUP_WINDOW = 600  # sn
KEEP = 3600
MAX_SENDER, MAX_TEXT, MAX_RECEIVED = 200, 20000, 64

OTP_WORD = re.compile(r"\b(kod|code|sifre|dogrulama|verification|otp|parola|onay)")
OTP_CODE = re.compile(r"(?<!\d)(\d{4,8}|\d{3}[ -]\d{3})(?!\d)")
ONLY_CODE = re.compile(r"\W*(?:[a-z]{1,3}-?)?\d{4,8}\W*")  # "482913", "G-482913"


def sms_dir() -> Path:
    return Path(os.environ.get("CFV_SMS_DIR", "/mnt/depo1/cfv/data/raw/sms-shortcut"))


def fold(s: str) -> str:
    """Kural eşleşmesi için: küçük harf, aksan/nokta yok, ı→i ('ŞİFRE' → 'sifre')."""
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch)).replace("ı", "i")


def is_otp(text: str) -> bool:
    f = fold(text)
    return bool(OTP_WORD.search(f) and OTP_CODE.search(f)) or bool(ONLY_CODE.fullmatch(f))


def norm_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def dup_key(sender: str, text: str) -> str:
    return hashlib.sha256(f"{sender.strip()}\x00{norm_text(text)}".encode()).hexdigest()


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _month(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m")


class SmsIngest:
    def __init__(self, state: Path | None = None, out_dir: Path | None = None, clock=time.time):
        self.state = Path(state) if state else state_dir()
        self.token_file = self.state / "sms-token"
        self.count_file = self.state / "sms-otp-count.json"
        self.out_dir = Path(out_dir) if out_dir else sms_dir()
        self.clock = clock
        self.lock = threading.Lock()
        self.seen: dict[str, float] = {}      # yazılan anahtar → son yazılış
        self.seen_otp: dict[str, float] = {}  # yalnız sayaç tekrar saymasın diye (metin tutulmaz)
        self._loaded = False

    # ------------------------------------------------------------ token
    def configured(self) -> bool:
        return self.token_file.is_file()

    def check(self, header: str | None) -> bool:
        if not header or not header.startswith("Bearer "):
            return False
        given = header[7:].strip()
        if not given or len(given) > 200:
            return False
        try:
            stored = self.token_file.read_text().strip()
        except OSError:
            return False
        return hmac.compare_digest(hashlib.sha256(given.encode()).hexdigest(), stored)

    def new_token(self) -> str:
        tok = secrets.token_urlsafe(32)  # 32 rastgele bayt
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.token_file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(hashlib.sha256(tok.encode()).hexdigest() + "\n")
        os.replace(tmp, self.token_file)
        return tok

    # ------------------------------------------------------------ kayıt
    def _prune(self, now: float):
        for d in (self.seen, self.seen_otp):
            for k in [k for k, t in d.items() if now - t > KEEP]:
                del d[k]

    def _load(self, now: float):
        """Yeniden başlatmadan sonra tekrarları tanımak için son saatin satırlarını oku."""
        self._loaded = True
        for month in sorted({_month(now - KEEP), _month(now)}):
            p = self.out_dir / f"{month}.jsonl"
            try:
                fh = p.open(encoding="utf-8")
            except OSError:
                continue
            with fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                        t = dt.datetime.strptime(r["alindi_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(
                            tzinfo=dt.timezone.utc).timestamp()
                        if now - t <= KEEP:
                            k = dup_key(r["sender"], r["text"])
                            self.seen[k] = max(t, self.seen.get(k, 0))
                    except (ValueError, KeyError, TypeError):
                        continue

    def _count_otp(self, now: float):
        try:
            counts = json.loads(self.count_file.read_text())
        except (OSError, ValueError):
            counts = {}
        m = _month(now)
        counts[m] = int(counts.get(m, 0)) + 1
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.count_file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(counts, f)
        os.replace(tmp, self.count_file)

    def _append(self, now: float, row: dict):
        p = self.out_dir / f"{_month(now)}.jsonl"
        line = (json.dumps(row, ensure_ascii=False) + "\n").encode()
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)

    def ingest(self, data) -> str:
        """→ 'yazildi' | 'tekrar' | 'otp'. Geçersiz gövdede ValueError."""
        if not isinstance(data, dict):
            raise ValueError("gövde")
        sender, text, rec = data.get("sender"), data.get("text"), data.get("received_at")
        if not isinstance(sender, str) or not sender.strip() or len(sender) > MAX_SENDER:
            raise ValueError("sender")
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            raise ValueError("text")
        if rec is not None and not isinstance(rec, (str, int, float)):
            raise ValueError("received_at")
        rec = None if rec is None else str(rec)[:MAX_RECEIVED]
        key = dup_key(sender, text)
        with self.lock:
            now = self.clock()
            if not self._loaded:
                self._load(now)
            self._prune(now)
            if is_otp(text):
                if now - self.seen_otp.get(key, -1e18) > DUP_WINDOW:
                    self._count_otp(now)
                    self.seen_otp[key] = now
                return "otp"
            if now - self.seen.get(key, -1e18) <= DUP_WINDOW:
                return "tekrar"
            self._append(now, {"received_at": rec, "sender": sender.strip(), "text": text,
                               "alindi_utc": _iso(now)})
            self.seen[key] = now
            return "yazildi"


def sms_token_cli() -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("Terminalde çalıştır: token yalnız ekrana basılır (pipe/dosyaya yazılmaz).", file=sys.stderr)
        return 2
    s = SmsIngest()
    if s.configured():
        if input("Mevcut SMS token'ı geçersiz olacak, iPhone'daki otomasyonları güncellemen gerekecek. "
                 "Devam? [e/H] ").strip().lower() not in ("e", "evet"):
            print("Değişmedi.")
            return 1
    tok = s.new_token()
    print("\nSMS token (yalnız burada gösterilir, Kestirmeler'de 'Authorization: Bearer <token>'):\n")
    print(tok)
    print(f"\nSunucuda yalnız sha256'sı var: {s.token_file}. Uç artık AÇIK (/ingest/sms).")
    return 0
