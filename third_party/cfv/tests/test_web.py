"""cfv-site testleri — tamamen SENTETİK veri (cfv_site.synth)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "site"))  # cfv_site (pyproject dışı)

import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import pytest

from cfv_site import auth as authmod
from cfv_site import match, synth
from cfv_site.data import Store
from cfv_site.decisions import Decisions
from cfv_site.server import App, make_handler

PW = "test-sifresi-uzun-ve-sentetik-123"


@pytest.fixture(scope="module")
def dbfile(tmp_path_factory):
    p = tmp_path_factory.mktemp("cfv") / "cfv.sqlite"
    synth.generate(p, scale=0.3, seed=3)
    return p


@pytest.fixture
def store(dbfile, tmp_path):
    return Store(dbfile, Decisions(tmp_path / "decisions.sqlite"))


# ------------------------------------------------------------------ eşleşme
def test_norm_name_turkish():
    assert match.norm_name("İREM Işık") == match.norm_name("irem ışık") == "irem isik"
    assert match.norm_name("Çağla 🌸") == "cagla"


def test_phone_digits():
    assert match.phone_digits("900001112233@s.whatsapp.net") == "900001112233"
    assert match.phone_digits("+90 000 111 22 33") == "900001112233"
    assert match.phone_digits("123456789-1600000000@g.us") is None


def _al(sk, alias, name, pid):
    return match.Alias((sk, alias), pid, name, match.norm_name(name))


def test_different_blocks_transitive_merge():
    a = _al("contacts", "+900001112233", "Aslı Kaya", 1)
    b = _al("whatsapp", "900001112233@s.whatsapp.net", "Aslı Kaya", 2)
    c = _al("iphone-photos", "U1", "Aslı Kaya", 3)
    aliases = {x.key: x for x in (a, b, c)}
    cands = match.compute_candidates(aliases)
    methods = {(x.a, x.b): x.method for x in cands}
    assert methods[match._pair(a.key, b.key)] == "contacts_phone"
    # Tuna: foto yüzü rehberdeki kişi değil → telefonla otomatik birleşen WhatsApp üzerinden de birleşmemeli
    eff = {match._pair(a.key, c.key): {"id": 1, "verdict": "different"}}
    r = match.build(aliases, cands, eff)
    assert r["group_of"][a.key] == r["group_of"][b.key]
    assert r["group_of"][c.key] != r["group_of"][a.key]
    assert r["status"][match._pair(b.key, c.key)] in ("pending", "blocked")


# ------------------------------------------------------------------ kararlar
def test_decisions_never_delete(tmp_path):
    d = Decisions(tmp_path / "d.sqlite")
    a, b = ("contacts", "+1"), ("whatsapp", "1@s")
    i1 = d.add(a, b, "same")
    i2 = d.add(b, a, "different")  # sıra fark etmez, öncekini geri alınmış sayar
    assert list(d.effective().values())[0]["id"] == i2
    assert d.undo(i2)
    assert d.effective() == {}
    rows = d.all()
    assert [r["id"] for r in rows] == [i1, i2] and all(r["undone_at"] for r in rows)
    assert (tmp_path / "d.sqlite").stat().st_mode & 0o777 == 0o600


def test_decision_survives_regenerated_db(dbfile, tmp_path):
    """Kararlar alias'a bağlı: DB baştan üretilse de (person.id'ler değişse de) geçerli kalır."""
    dec = Decisions(tmp_path / "d.sqlite")
    s1 = Store(dbfile, dec)
    item = s1.matches("pending")["items"][0]
    a, b = (item["a"]["kind"], item["a"]["alias"]), (item["b"]["kind"], item["b"]["alias"])
    s1.decide(a, b, "same")
    db2 = tmp_path / "again.sqlite"
    synth.generate(db2, scale=0.3, seed=3)
    m2 = Store(db2, dec).model()
    assert m2.canon_of_key[a] == m2.canon_of_key[b]


# ------------------------------------------------------------------ veri
def test_window_and_person(store):
    meta = store.meta()
    assert meta["synthetic"] and meta["first"] <= meta["last"]
    w = store.window("2024-01-01", "2024-12-31")
    types = {n["type"] for n in w["nodes"]}
    assert {"person", "calendar", "photos"} <= types
    ids = {n["id"] for n in w["nodes"]}
    assert all(l["s"] in ids and l["t"] in ids for l in w["links"])
    pid = int(next(n for n in w["nodes"] if n["type"] == "person")["id"][2:])
    p = store.person(pid)
    assert p["name"] and p["aliases"] and p["weeks"]


def test_link_and_split(store):
    m = store.model()
    singles = [c for c, keys in m.members.items() if len(keys) == 1][:2]
    did = store.link_people(*singles)
    m2 = store.model()
    assert m2.canon_of_key[m.members[singles[0]][0]] == m2.canon_of_key[m.members[singles[1]][0]]
    store.decisions.undo(did)
    m3 = store.model()
    assert m3.canon_of_key[m.members[singles[0]][0]] != m3.canon_of_key[m.members[singles[1]][0]]


# ------------------------------------------------------------------ giriş
def test_password_hash_and_sessions(tmp_path):
    a = authmod.Auth(tmp_path / "st")
    with pytest.raises(ValueError):
        a.set_password("kisa")
    a.set_password(PW)
    assert PW not in (tmp_path / "st" / "password").read_text()
    assert (tmp_path / "st" / "password").stat().st_mode & 0o777 == 0o600
    tok = a.login("1.2.3.4", PW)
    assert tok and a.valid(tok)
    a.set_password(PW + "x")  # şifre değişince eski oturum düşer
    assert not a.valid(tok)


def test_lockout(tmp_path):
    a = authmod.Auth(tmp_path / "st")
    a.set_password(PW)
    for _ in range(authmod.IP_FAILS):
        assert a.login("9.9.9.9", "yanlis") is None
    assert a.locked("9.9.9.9")
    assert a.login("9.9.9.9", PW) is None  # kilitliyken doğru şifre de olmaz
    assert a.login("8.8.8.8", PW)  # başka IP etkilenmez


# ------------------------------------------------------------------ HTTP
@pytest.fixture
def server(store, tmp_path):
    a = authmod.Auth(tmp_path / "st")
    a.set_password(PW)
    app = App(store, a)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    port = httpd.server_address[1]
    app.origins = {f"http://127.0.0.1:{port}"}
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield port
    httpd.shutdown()


def req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r, data


PROTECTED = ["/", "/index.html", "/static/app.js", "/static/style.css", "/static/d3-force.bundle.min.js",
             "/api/meta", "/api/window?from=2024-01-01&to=2024-02-01", "/api/density?from=2024-01-01&to=2024-02-01",
             "/api/person/1", "/api/thumb/1", "/api/photos?from=2024-01-01&to=2024-02-01",
             "/api/matches", "/api/search?q=a", "/api/event/1", "/olmayan"]


def test_nothing_without_session(server):
    for p in PROTECTED:
        r, data = req(server, "GET", p)
        assert r.status in (303, 401), p
        assert len(data) < 40, p
        assert r.getheader("X-Robots-Tag") is None  # nginx ekler (çift başlık olmasın)
    r, data = req(server, "GET", "/robots.txt")
    assert b"Disallow: /" in data
    # sahte çerez
    r, _ = req(server, "GET", "/api/meta", headers={"Cookie": f"{authmod.COOKIE}=uydurma"})
    assert r.status == 401


def login(port, pw=PW, origin=None):
    origin = origin or f"http://127.0.0.1:{port}"
    return req(port, "POST", "/login", urlencode({"password": pw}),
               {"Origin": origin, "Content-Type": "application/x-www-form-urlencoded"})


def test_login_flow(server):
    r, _ = login(server, origin="https://evil.test")
    assert r.status == 403
    r, _ = login(server, pw="yanlis")
    assert r.status == 303 and r.getheader("Location").endswith("e=1") and not r.getheader("Set-Cookie")
    r, _ = login(server)
    sc = r.getheader("Set-Cookie")
    for flag in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
        assert flag in sc
    assert sc.startswith(authmod.COOKIE + "=")
    cookie = sc.split(";")[0]
    for p in PROTECTED[:-1]:
        if "person/1" in p or "event/1" in p or "thumb/1" in p:
            continue
        r2, _ = req(server, "GET", p, headers={"Cookie": cookie})
        assert r2.status == 200, p
    r2, body = req(server, "GET", "/api/window?from=2024-01-01&to=2024-06-30", headers={"Cookie": cookie})
    assert json.loads(body)["nodes"]
    # POST karar: Origin'siz ret, Origin'li kabul
    m = json.loads(req(server, "GET", "/api/matches", headers={"Cookie": cookie})[1])["items"][0]
    body = json.dumps({"a": m["a"], "b": m["b"], "verdict": "same"})
    r3, _ = req(server, "POST", "/api/decision", body, {"Cookie": cookie, "Content-Type": "application/json"})
    assert r3.status == 403
    r3, d = req(server, "POST", "/api/decision", body, {"Cookie": cookie, "Content-Type": "application/json",
                                                        "Origin": f"http://127.0.0.1:{server}"})
    assert r3.status == 200 and json.loads(d)["id"]
    # çıkış → oturum düşer
    req(server, "POST", "/logout", "", {"Cookie": cookie, "Origin": f"http://127.0.0.1:{server}"})
    assert req(server, "GET", "/api/meta", headers={"Cookie": cookie})[0].status == 401


def test_require_synthetic_refuses_real_db(tmp_path):
    """Yayındaki site yalnız sentetik veri sunar: meta.synthetic yoksa model kurulmaz."""
    from cfv import db as cdb
    from cfv_site.data import NotSynthetic
    real = cdb.init_db(tmp_path / "gercek.sqlite")
    s = Store(real, Decisions(tmp_path / "d.sqlite"), require_synthetic=True)
    with pytest.raises(NotSynthetic):
        s.model()


def test_require_synthetic_accepts_synth(dbfile, tmp_path):
    assert Store(dbfile, Decisions(tmp_path / "d.sqlite"), require_synthetic=True).meta()["synthetic"]


def test_ready_preview_used(tmp_path, monkeypatch):
    from cfv_site import server as srv
    monkeypatch.setenv("CFV_DATA", str(tmp_path))
    small = tmp_path / "processed" / "photos" / "small"
    small.mkdir(parents=True)
    (small / "ABCDEF12-3456.jpg").write_bytes(b"\xff\xd8jpg")
    assert srv.ready_preview("ABCDEF12-3456", 240) == small / "ABCDEF12-3456.jpg"
    assert srv.ready_preview("ABCDEF12-3456", 720) is None       # 768'lik yok → üretilecek
    assert srv.ready_preview("../../etc/passwd", 240) is None    # yol dolaşma yok


# ------------------------------------------------------------------ SMS modülü (KULLANILMIYOR, server-sms'e geçti)
from cfv_site import sms as smsmod  # noqa: E402

OTP = ["Doğrulama kodunuz: 482913", "Your verification code is 123456", "G-482913 Google doğrulama kodunuzdur.",
       "ŞİFRENİZ 5521", "OTP 12345678 ile işlemi onaylayın", "482913", "G-4829", "123 456 is your code",
       "Parola: 90817 (kimseyle paylaşmayın)", "ONAY KODU 7788"]
NOT_OTP = ["Yarın 1430'da buluşalım mı?", "Kargonuz yola çıktı, takip no 1234567890", "Toplantı onaylandı",
           "Kod yazmayı bitirdim, akşam bakarız", "Merhaba, nasılsın?", "Fatura tutarı 245,90 TL"]


def test_otp_rule():
    for t in OTP:
        assert smsmod.is_otp(t), t
    for t in NOT_OTP:
        assert not smsmod.is_otp(t), t


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def _rows(d):
    return [json.loads(l) for f in sorted(d.glob("*.jsonl")) for l in f.read_text().splitlines()]


def test_sms_ingest_dedup_otp_reload(tmp_path):
    st, out, clk = tmp_path / "st", tmp_path / "sms", Clock()
    out.mkdir()
    s = smsmod.SmsIngest(st, out, clk)
    msg = {"sender": "+900001112233", "text": "Akşam  yemeğe gel", "received_at": "2026-11-09 21:00"}
    assert s.ingest(msg) == "yazildi"
    assert s.ingest({**msg, "text": " Akşam yemeğe gel "}) == "tekrar"   # boşluk farkı aynı mesaj
    clk.t += 9 * 60
    assert s.ingest(msg) == "tekrar"
    assert s.ingest({**msg, "sender": "+900009998877"}) == "yazildi"     # başka gönderen
    assert s.ingest({"sender": "Banka", "text": OTP[0]}) == "otp"
    assert s.ingest({"sender": "Banka", "text": OTP[0]}) == "otp"
    rows = _rows(out)
    assert len(rows) == 2 and all(OTP[0] not in json.dumps(r, ensure_ascii=False) for r in rows)
    assert set(rows[0]) == {"received_at", "sender", "text", "alindi_utc"}
    assert json.loads((st / "sms-otp-count.json").read_text()) == {smsmod._month(clk.t): 1}
    f = next(out.glob("*.jsonl"))
    assert f.stat().st_mode & 0o777 == 0o600
    # yeniden başlatma: dosyadan yüklenir, 10 dk dolmadan tekrar yazılmaz
    clk.t += 60
    s2 = smsmod.SmsIngest(st, out, clk)
    assert s2.ingest({**msg, "sender": "+900009998877"}) == "tekrar"
    clk.t += 11 * 60
    assert s2.ingest({**msg, "sender": "+900009998877"}) == "yazildi"
    assert len(_rows(out)) == 3
    for bad in ([], {"text": "x"}, {"sender": "", "text": "x"}, {"sender": "a", "text": 5},
                {"sender": "a", "text": "x", "received_at": {"a": 1}}):
        with pytest.raises(ValueError):
            s2.ingest(bad)


@pytest.fixture
def sms_server(store, tmp_path):
    out = tmp_path / "sms"
    out.mkdir()
    s = smsmod.SmsIngest(tmp_path / "st", out)
    app = App(store, authmod.Auth(tmp_path / "st"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], s, out
    httpd.shutdown()


@pytest.mark.skip(reason="SMS işi server-sms'e geçti; /ingest/sms cfv-site'a bağlı değil")
def test_sms_http(sms_server, capsys):
    port, s, out = sms_server
    body = json.dumps({"sender": "+900001112233", "text": "gizli-sentetik-metin", "received_at": "dün"})
    hdr = {"Content-Type": "application/json"}
    r, d = req(port, "POST", "/ingest/sms", body, {**hdr, "Authorization": "Bearer x"})
    assert r.status == 503                                            # token dosyası yok → KAPALI
    tok = s.new_token()
    assert tok not in (tmp := s.token_file.read_text()) and len(tmp.strip()) == 64
    assert s.token_file.stat().st_mode & 0o777 == 0o600
    for auth in (None, "Bearer yanlis", tok, "Basic " + tok):
        r, _ = req(port, "POST", "/ingest/sms", body, {**hdr, **({"Authorization": auth} if auth else {})})
        assert r.status == 401, auth
    ok = {**hdr, "Authorization": f"Bearer {tok}"}
    r, d = req(port, "POST", "/ingest/sms", body, ok)                 # Origin yok, çerez yok: kabul
    assert r.status == 200 and json.loads(d) == {"ok": True, "durum": "yazildi"}
    r, d = req(port, "POST", "/ingest/sms", body, ok)
    assert r.status == 200 and json.loads(d)["durum"] == "tekrar"
    r, _ = req(port, "POST", "/ingest/sms", "bozuk{", ok)
    assert r.status == 400
    assert len(_rows(out)) == 1
    # uç site verisine kapı açmaz
    assert req(port, "GET", "/api/meta", headers={"Authorization": f"Bearer {tok}"})[0].status == 401
    log = capsys.readouterr().err
    assert "/ingest/sms 200" in log and "gizli-sentetik-metin" not in log and tok not in log and "900001112233" not in log
