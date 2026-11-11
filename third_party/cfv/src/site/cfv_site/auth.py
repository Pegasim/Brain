"""Tek kullanıcılı giriş: scrypt şifre hash'i, sunucu tarafı oturumlar, deneme sınırı.

Durum dizini (CFV_SITE_STATE, varsayılan /var/lib/cfv-site, 700):
  password        scrypt$n$r$p$tuz_hex$hash_hex  (600) — şifrenin kendisi hiçbir yerde yok
  sessions.sqlite oturum token'larının yalnız sha256'sı

Şifre yalnız `src/site/cfv-site set-password` ile, getpass üzerinden girilir (argüman/env kabul edilmez).
"""

from __future__ import annotations

import datetime as dt
import getpass
import hashlib
import hmac
import os
import secrets
import sqlite3
import sys
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

COOKIE = "__Host-cfv_session"
SESSION_DAYS = 30
MIN_LEN = 16
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
# deneme sınırı (nginx limit_req + fail2ban'e ek ikinci katman)
IP_FAILS, IP_WINDOW, IP_LOCK = 5, 15 * 60, 15 * 60
GLOBAL_FAILS, GLOBAL_WINDOW, GLOBAL_DELAY = 30, 3600, 5.0


def state_dir() -> Path:
    return Path(os.environ.get("CFV_SITE_STATE", "/var/lib/cfv-site"))


def _now() -> int:
    return int(time.time())


def _hash(pw: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(pw.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 * r * n * 2, dklen=32)


def make_hash(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = _hash(pw, salt, **SCRYPT)
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${salt.hex()}${h.hex()}"


def check_hash(pw: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, h = stored.strip().split("$")
        if algo != "scrypt":
            return False
        return hmac.compare_digest(_hash(pw, bytes.fromhex(salt), int(n), int(r), int(p)), bytes.fromhex(h))
    except (ValueError, TypeError):
        return False


class Auth:
    def __init__(self, root: Path | None = None):
        self.root = root or state_dir()
        self.pw_file = self.root / "password"
        self.db_file = self.root / "sessions.sqlite"
        self._lock = threading.Lock()
        self._ip_fails: dict[str, deque] = defaultdict(deque)
        self._ip_locked: dict[str, int] = {}
        self._global_fails: deque = deque()

    # -- şifre
    def configured(self) -> bool:
        return self.pw_file.is_file() and self.pw_file.stat().st_size > 0

    def _stored(self) -> str:
        return self.pw_file.read_text().strip()

    def _epoch(self) -> str:
        """Şifre değişince eski oturumlar geçersiz olsun diye: hash'in parmak izi."""
        return hashlib.sha256(self._stored().encode()).hexdigest()[:16]

    def set_password(self, pw: str):
        if len(pw) < MIN_LEN:
            raise ValueError(f"şifre en az {MIN_LEN} karakter olmalı")
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        tmp = self.pw_file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(make_hash(pw) + "\n")
        os.replace(tmp, self.pw_file)
        with self._db() as c:  # tüm eski oturumları iptal et
            c.execute("UPDATE session SET revoked_at = ? WHERE revoked_at IS NULL", (_now(),))

    # -- oturumlar
    def _db(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        new = not self.db_file.exists()
        c = sqlite3.connect(self.db_file)
        if new:
            os.chmod(self.db_file, 0o600)
        c.execute("""CREATE TABLE IF NOT EXISTS session (
            token_sha TEXT PRIMARY KEY, epoch TEXT NOT NULL, created_at INTEGER NOT NULL,
            last_seen INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER)""")
        return c

    @staticmethod
    def _sha(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        now = _now()
        with self._db() as c:
            c.execute("INSERT INTO session VALUES (?,?,?,?,?,NULL)",
                      (self._sha(token), self._epoch(), now, now, now + SESSION_DAYS * 86400))
        return token

    def valid(self, token: str | None) -> bool:
        if not token or not self.configured() or len(token) > 100:
            return False
        now = _now()
        with self._db() as c:
            r = c.execute("SELECT epoch, expires_at, revoked_at, last_seen FROM session WHERE token_sha = ?",
                          (self._sha(token),)).fetchone()
            if not r or r[2] is not None or r[1] < now or r[0] != self._epoch():
                return False
            if now - r[3] > 300:
                c.execute("UPDATE session SET last_seen = ? WHERE token_sha = ?", (now, self._sha(token)))
        return True

    def revoke(self, token: str | None):
        if token:
            with self._db() as c:
                c.execute("UPDATE session SET revoked_at = ? WHERE token_sha = ? AND revoked_at IS NULL",
                          (_now(), self._sha(token)))

    # -- giriş denemesi
    def locked(self, ip: str) -> int:
        """Kilitliyse kalan saniye, değilse 0."""
        with self._lock:
            until = self._ip_locked.get(ip, 0)
            return max(0, until - _now())

    def login(self, ip: str, pw: str) -> str | None:
        """Doğruysa yeni oturum token'ı, değilse None. Kilitli IP için hiç denemez."""
        if not self.configured() or self.locked(ip):
            return None
        with self._lock:
            now = _now()
            while self._global_fails and self._global_fails[0] < now - GLOBAL_WINDOW:
                self._global_fails.popleft()
            slow = len(self._global_fails) >= GLOBAL_FAILS
        if slow:
            time.sleep(GLOBAL_DELAY)
        if pw and len(pw) <= 1024 and check_hash(pw, self._stored()):
            with self._lock:
                self._ip_fails.pop(ip, None)
            return self.new_session()
        with self._lock:
            now = _now()
            q = self._ip_fails[ip]
            q.append(now)
            while q and q[0] < now - IP_WINDOW:
                q.popleft()
            if len(q) >= IP_FAILS:
                self._ip_locked[ip] = now + IP_LOCK
                q.clear()
            self._global_fails.append(now)
        print(f"login_failed ip={ip}", file=sys.stderr, flush=True)
        return None


def set_password_cli() -> int:
    if not sys.stdin.isatty():
        print("Terminalde çalıştır (şifre getpass ile sorulur, argüman/pipe kabul edilmez).", file=sys.stderr)
        return 2
    a = Auth()
    pw = getpass.getpass(f"Yeni şifre (en az {MIN_LEN} karakter): ")
    if pw != getpass.getpass("Tekrar: "):
        print("Eşleşmedi, değişmedi.", file=sys.stderr)
        return 1
    try:
        a.set_password(pw)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"Tamam: hash {a.pw_file} dosyasında; eski oturumlar kapatıldı.")
    return 0


if __name__ == "__main__":
    sys.exit(set_password_cli())
