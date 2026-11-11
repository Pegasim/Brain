"""cfv-site HTTP sunucusu (stdlib). Yalnız 127.0.0.1 dinler; dışa açma kararı Tuna'da.

    src/site/cfv-site serve            # 127.0.0.1:3050 (CFV_SITE_HOST / CFV_SITE_PORT)
    src/site/cfv-site set-password     # şifre (getpass), yalnız hash saklanır

Oturumsuz açılabilen yalnız: GET /login, POST /login, GET /robots.txt. Geri kalan her yol
(sayfa, JS/CSS, API, fotoğraf, küçük resim) oturum ister; yoksa 303 /login ya da 401, gövdede veri yok.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cfv import db
from cfv_site import auth as authmod
from cfv_site.data import NotSynthetic, Store

STATIC = Path(__file__).parent / "static"
STATIC_FILES = {"app.js": "text/javascript", "style.css": "text/css",
                "d3-force.bundle.min.js": "text/javascript"}
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
THUMB_SIZES = (240, 720)
_thumb_sem = threading.Semaphore(2)  # /mnt/depo1 SMR: aynı anda en çok 2 dönüştürme

CSP_APP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
           "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def thumbs_dir() -> Path:
    return Path(os.environ.get("CFV_SITE_THUMBS", db.data_dir() / "processed" / "site-thumbs"))


def allowed_origins() -> set[str]:
    port = os.environ.get("CFV_SITE_PORT", "3050")
    default = f"https://cfv.tunapro.xyz,http://127.0.0.1:{port},http://localhost:{port}"
    return {o.strip() for o in os.environ.get("CFV_SITE_ORIGINS", default).split(",") if o.strip()}


LOGIN_HTML = """<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>CFV — giriş</title><style nonce="{nonce}">
:root{{color-scheme:dark}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#17181c;
color:#d8d8dc;font:15px/1.4 system-ui,sans-serif}}form{{width:min(320px,calc(100vw - 32px));display:grid;gap:12px}}
h1{{font-size:18px;font-weight:600;margin:0}}.slogan{{color:#8b8d96;font-style:italic;font-size:13px;margin:-8px 0 4px}}input{{font:inherit;padding:10px 12px;border-radius:8px;
border:1px solid #3a3c44;background:#202228;color:inherit}}button{{font:inherit;padding:10px;border-radius:8px;
border:0;background:#6d8cff;color:#0d0f14;font-weight:600;cursor:pointer}}p{{margin:0;color:#ff8f8f;font-size:13px}}
</style></head><body><form method="post" action="/login"><h1>CFV</h1><div class="slogan">put yourself in a flash disk</div>{msg}
<input type="password" name="password" autocomplete="current-password" autofocus required aria-label="Şifre"
placeholder="Şifre"><button>Giriş</button></form></body></html>"""

NOT_CONFIGURED = """<!doctype html><meta charset="utf-8"><meta name="robots" content="noindex">
<title>CFV</title><p>Şifre ayarlanmamış.</p>"""


class App:
    def __init__(self, store: Store | None = None, auth: authmod.Auth | None = None):
        self.store = store or Store()
        self.auth = auth or authmod.Auth()
        self.origins = allowed_origins()


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        server_version = "cfv-site"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        # ---------------------------------------------------------- yardımcılar
        def log_request(self, code="-", size="-"):
            # sorgu dizesi (arama kelimesi, kişi adı) loglanmaz
            path = urlsplit(self.path).path
            dur = (time.monotonic() - getattr(self, "_t0", time.monotonic())) * 1000
            sys.stderr.write(f"{self.command} {path} {code} {dur:.0f}ms\n")

        def log_message(self, fmt, *args):
            pass

        def client_ip(self) -> str:
            ip = self.client_address[0]
            real = self.headers.get("X-Real-IP")
            if ip in ("127.0.0.1", "::1") and real:
                return real.strip()[:64]
            return ip

        def token(self) -> str | None:
            raw = self.headers.get("Cookie")
            if not raw:
                return None
            try:
                c = cookies.SimpleCookie(raw)
            except cookies.CookieError:
                return None
            m = c.get(authmod.COOKIE)
            return m.value if m else None

        def send(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8",
                 headers: dict | None = None, cache: str = "no-store", csp: str = CSP_APP):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            # X-Robots-Tag ve HSTS nginx vhost'unda (çift gelmesin); uygulamada robots.txt + <meta robots> kalır.
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy", csp)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def json(self, obj, code: int = 200):
            self.send(code, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode(),
                      "application/json; charset=utf-8")

        def redirect(self, to: str, headers: dict | None = None):
            self.send(303, b"", headers={"Location": to, **(headers or {})})

        def deny(self, path: str):
            if path == "/" or (self.headers.get("Accept", "").startswith("text/html") and not path.startswith("/api/")):
                self.redirect("/login")
            else:
                self.send(401, b'{"error":"giris"}', "application/json")

        def same_origin(self) -> bool:
            """CSRF: POST yalnız bu siteden. Sec-Fetch-Site varsa 'same-origin' olmalı; Origin varsa
            (ve 'null' değilse) izinli listede olmalı; ikisi de yoksa ret."""
            origin = self.headers.get("Origin")
            site = self.headers.get("Sec-Fetch-Site")
            ok = True
            if site is not None and site != "same-origin":
                ok = False
            if origin not in (None, "null") and origin not in app.origins:
                ok = False
            if site is None and origin in (None, "null"):
                ok = False
            if not ok:
                sys.stderr.write(f"origin_rejected origin={(origin or '-')[:80]} site={site or '-'}\n")
            return ok

        def read_body(self, limit: int = 65536) -> bytes:
            """Gövde her POST'ta baştan okunur: erken yanıtta kalan baytlar bağlantıyı bozmasın."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > limit:
                self.close_connection = True
                return b""
            return self.rfile.read(n) if n else b""

        # ---------------------------------------------------------- yönlendirme
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            self._t0 = time.monotonic()
            u = urlsplit(self.path)
            path, q = u.path, {k: v[-1] for k, v in parse_qs(u.query).items()}
            try:
                if path == "/robots.txt":
                    return self.send(200, b"User-agent: *\nDisallow: /\n", cache="public, max-age=86400")
                if path == "/login":
                    return self.login_page(q)
                if not app.auth.valid(self.token()):
                    return self.deny(path)
                return self.authed_get(path, q)
            except (ValueError, KeyError):
                return self.json({"error": "istek"}, 400)
            except NotSynthetic:
                sys.stderr.write("REDDEDİLDİ: DB sentetik değil (CFV_SITE_REQUIRE_SYNTHETIC=1)\n")
                return self.json({"error": "veri yok"}, 503)
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"hata {path}: {type(e).__name__}\n")
                return self.json({"error": "sunucu"}, 500)

        def do_POST(self):
            self._t0 = time.monotonic()
            path = urlsplit(self.path).path
            self._body = self.read_body()
            try:
                if not self.same_origin():
                    return self.send(403, b"origin")
                if path == "/login":
                    return self.login_post()
                if path == "/logout":
                    app.auth.revoke(self.token())
                    return self.redirect("/login", {"Set-Cookie": f"{authmod.COOKIE}=; Path=/; Secure; HttpOnly; "
                                                                  f"SameSite=Lax; Max-Age=0"})
                if not app.auth.valid(self.token()):
                    return self.send(401, b'{"error":"giris"}', "application/json")
                data = json.loads(self._body or b"{}")
                return self.authed_post(path, data)
            except (ValueError, KeyError, TypeError):
                return self.json({"error": "istek"}, 400)
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"hata {path}: {type(e).__name__}\n")
                return self.json({"error": "sunucu"}, 500)

        # ---------------------------------------------------------- giriş
        def login_page(self, q):
            if not app.auth.configured():
                return self.send(503, NOT_CONFIGURED.encode(), "text/html; charset=utf-8")
            if app.auth.valid(self.token()):
                return self.redirect("/")
            nonce = secrets.token_urlsafe(16)
            msg = {"1": "<p>Şifre yanlış.</p>", "2": "<p>Çok fazla deneme. Biraz sonra tekrar dene.</p>"}.get(q.get("e"), "")
            page = LOGIN_HTML.format(nonce=nonce, msg=msg).encode()
            csp = (f"default-src 'none'; style-src 'nonce-{nonce}'; form-action 'self'; "
                   f"frame-ancestors 'none'; base-uri 'none'")
            self.send(200, page, "text/html; charset=utf-8", csp=csp)

        def login_post(self):
            ip = self.client_ip()
            if app.auth.locked(ip):
                return self.redirect("/login?e=2")
            pw = parse_qs(self._body[:4096].decode("utf-8", "replace")).get("password", [""])[-1]
            tok = app.auth.login(ip, pw)
            if not tok:
                return self.redirect("/login?e=2" if app.auth.locked(ip) else "/login?e=1")
            cookie = (f"{authmod.COOKIE}={tok}; Path=/; Secure; HttpOnly; SameSite=Lax; "
                      f"Max-Age={authmod.SESSION_DAYS * 86400}")
            self.redirect("/", {"Set-Cookie": cookie})

        # ---------------------------------------------------------- oturumlu
        def authed_get(self, path: str, q: dict):
            s = app.store
            if path in ("/", "/index.html"):
                return self.send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8",
                                 cache="private, no-cache")
            if path.startswith("/static/"):
                name = path[len("/static/"):]
                if name not in STATIC_FILES:
                    return self.send(404, b"yok")
                return self.send(200, (STATIC / name).read_bytes(), STATIC_FILES[name] + "; charset=utf-8",
                                 cache="private, no-cache")
            if path == "/api/meta":
                return self.json(s.meta())
            if path == "/api/density":
                return self.json(s.density(*self.range(q), q.get("step") if q.get("step") in ("day", "week", "month") else None))
            if path == "/api/window":
                return self.json(s.window(*self.range(q)))
            if path == "/api/photos":
                return self.json(s.photos(*self.range(q)))
            if path == "/api/search":
                return self.json(s.search_people(q.get("q", "")[:80]))
            if path == "/api/matches":
                st = q.get("status", "pending")
                if st not in ("pending", "auto", "same", "different", "blocked", "all"):
                    raise ValueError
                return self.json(s.matches(st))
            m = re.fullmatch(r"/api/person/(\d+)", path)
            if m:
                p = s.person(int(m.group(1)))
                return self.json(p) if p else self.json({"error": "yok"}, 404)
            m = re.fullmatch(r"/api/event/(\d+)", path)
            if m:
                e = s.calendar_event(int(m.group(1)))
                return self.json(e) if e else self.json({"error": "yok"}, 404)
            m = re.fullmatch(r"/api/thumb/(\d+)", path)
            if m:
                return self.thumb(int(m.group(1)), int(q.get("s", 240)))
            return self.json({"error": "yok"}, 404)

        def authed_post(self, path: str, d: dict):
            s = app.store
            if path == "/api/decision":
                a = (str(d["a"]["kind"]), str(d["a"]["alias"]))
                b = (str(d["b"]["kind"]), str(d["b"]["alias"]))
                if d["verdict"] not in ("same", "different"):
                    raise ValueError
                return self.json({"id": s.decide(a, b, d["verdict"], (d.get("note") or None))})
            m = re.fullmatch(r"/api/decision/(\d+)/undo", path)
            if m:
                return self.json({"ok": s.decisions.undo(int(m.group(1)))})
            if path == "/api/link":
                return self.json({"id": s.link_people(int(d["a"]), int(d["b"]))})
            return self.json({"error": "yok"}, 404)

        def range(self, q: dict) -> tuple[str, str]:
            d0, d1 = q.get("from", ""), q.get("to", "")
            if not (DATE.match(d0) and DATE.match(d1)) or d0 > d1:
                raise ValueError("tarih")
            return d0, d1

        # ---------------------------------------------------------- küçük resim
        def thumb(self, mid: int, size: int):
            size = min(THUMB_SIZES, key=lambda s: abs(s - size))
            info = app.store.media_path(mid)
            if not info:
                return self.send(404, b"yok")
            path, mime, day, uuid = info
            ready = ready_preview(uuid, size)
            if ready:
                return self.send(200, ready.read_bytes(), "image/jpeg", cache="private, max-age=604800")
            src = Path(path)
            if not src.is_file():
                return self.send(200, placeholder(mid, day, app.store.model().synthetic), "image/svg+xml",
                                 cache="private, max-age=86400")
            out = thumbs_dir() / f"{mid // 1000:04d}" / f"{mid}-{size}.jpg"
            if not out.is_file():
                with _thumb_sem:
                    if not out.is_file():
                        make_thumb(src, mime, out, size)
            if not out.is_file():
                return self.send(200, placeholder(mid, day, False), "image/svg+xml", cache="private, max-age=600")
            return self.send(200, out.read_bytes(), "image/jpeg", cache="private, max-age=604800")

    return H


UUID = re.compile(r"^[0-9A-Fa-f-]{8,64}$")


def ready_preview(uuid: str | None, size: int) -> Path | None:
    """Foto ajanının hazır önizlemeleri: photos/small (224 px) ve photos/thumbs (768 px)."""
    if not uuid or not UUID.match(uuid):
        return None
    base = db.data_dir() / "processed" / "photos"
    order = ("small", "thumbs") if size <= 240 else ("thumbs",)
    for sub in order:
        p = base / sub / f"{uuid}.jpg"
        if p.is_file():
            return p
    return None


def make_thumb(src: Path, mime: str, out: Path, size: int):
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.jpg")
    if mime.startswith("video/"):
        cmd = ["ffmpeg", "-v", "error", "-y", "-ss", "1", "-i", str(src), "-frames:v", "1",
               "-vf", f"scale={size}:{size}:force_original_aspect_ratio=decrease", str(tmp)]
    else:
        cmd = ["vipsthumbnail", str(src), "--size", f"{size}x{size}", "-o", f"{tmp}[Q=80,strip]"]
    try:
        subprocess.run(cmd, timeout=40, check=True, capture_output=True)
        os.replace(tmp, out)
    except (subprocess.SubprocessError, OSError):
        sys.stderr.write(f"thumb başarısız media={out.stem}\n")


def placeholder(mid: int, day: str, synthetic: bool) -> bytes:
    if synthetic:
        h = int(hashlib.md5(str(mid).encode()).hexdigest()[:6], 16)
        hue, hue2 = h % 360, (h // 360) % 360
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 180" width="240" height="180">'
                f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="hsl({hue},45%,42%)"/>'
                f'<stop offset="1" stop-color="hsl({hue2},50%,28%)"/></linearGradient></defs>'
                f'<rect width="240" height="180" fill="url(#g)"/><circle cx="{60 + h % 120}" cy="70" r="26" fill="hsla(0,0%,100%,.18)"/>'
                f'<text x="12" y="166" font-family="sans-serif" font-size="14" fill="#fff" opacity=".85">'
                f'{html.escape(day or "")} · örnek</text></svg>').encode()
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 180" width="240" height="180">'
            '<rect width="240" height="180" fill="#2a2c33"/><text x="120" y="95" text-anchor="middle" '
            'font-family="sans-serif" font-size="13" fill="#888">dosya yok</text></svg>').encode()


def serve(host: str, port: int):
    if host not in ("127.0.0.1", "::1", "localhost"):
        sys.exit("cfv-site yalnız loopback dinler (dışa açma nginx ile, Tuna kararıyla).")
    app = App()
    if app.store.require_synthetic:
        try:
            app.store.model()
        except NotSynthetic:
            sys.exit(f"DB sentetik değil, açılmadı: {app.store.db_path} (CFV_SITE_REQUIRE_SYNTHETIC=1; "
                     "gerçek veri Tuna onayı ister)")
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    if not app.auth.configured():
        sys.stderr.write("UYARI: şifre ayarlanmamış; site hiçbir veri dönmez. `src/site/cfv-site set-password`\n")
    sys.stderr.write(f"cfv-site http://{host}:{port}  db={app.store.db_path}\n")
    httpd.serve_forever()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cfv-site")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve")
    sp.add_argument("--host", default=os.environ.get("CFV_SITE_HOST", "127.0.0.1"))
    sp.add_argument("--port", type=int, default=int(os.environ.get("CFV_SITE_PORT", "3050")))
    sub.add_parser("set-password")
    a = ap.parse_args(argv)
    if a.cmd == "set-password":
        sys.exit(authmod.set_password_cli())

    serve(a.host, a.port)


if __name__ == "__main__":
    main()
