"""src/photos/icloud/indir.py: üzerine yazmama, boyut tutmazsa yerleştirmeme, exif farkını indirmeme.

icloudpd/pyicloud_ipd gerektirir (cfv .venv'inde yok → skip). Çalıştırma:
  /opt/icloudpd/lib-venv/bin/python -m pytest -q tests/test_icloud_indir.py
"""

import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pyicloud_ipd")
pytest.importorskip("icloudpd")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "photos" / "icloud"))
import icloud_yol as Y  # noqa: E402
import indir  # noqa: E402

NOW = dt.datetime(2025, 1, 5, 10, 0, tzinfo=dt.timezone.utc)


class FakeSession:
    """Range'e uyan sahte indirme oturumu; çağrıları sayar."""
    def __init__(self, data: bytes, status=None):
        self.data, self.status, self.calls = data, status, 0

    def get(self, url, headers=None, stream=True, timeout=None):
        self.calls += 1
        start = int(headers["Range"].split("=")[1].rstrip("-"))
        body = self.data[start:]
        code = self.status or (206 if start else 200)
        return SimpleNamespace(status_code=code, iter_content=lambda n: [body[i:i + n] for i in range(0, len(body), n)])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(Y, "ST", str(tmp_path / "st"))
    (tmp_path / "st").mkdir()
    monkeypatch.setattr(indir.time, "sleep", lambda s: None)
    return tmp_path


def _job(tmp_path, name, size):
    return Y.Job("original", str(tmp_path / "lib" / name), size, SimpleNamespace(url="https://x/y"))


def test_state_and_plan(env):
    lib = env / "lib"
    lib.mkdir()
    (lib / "A.JPG").write_bytes(b"a" * 5000)
    (lib / "B.JPG").write_bytes(b"b" * 5200)                    # exif farkı (200 bayt)
    (lib / "C.JPG").write_bytes(b"c" * 20000)                   # aynı ad, başka dosya
    (lib / "D.JPG").write_bytes(b"d" * 100)
    (lib / "D-5000.JPG").write_bytes(b"d" * 10)                 # '-<boyut>' var ama eksik
    assert Y.state(_job(env, "A.JPG", 5000))[0] == "tamam"
    assert Y.state(_job(env, "E.JPG", 5000))[0] == "eksik"
    assert Y.state(_job(env, "D.JPG", 5000))[0] == "yarim"
    what, job, st, _ = indir.plan(_job(env, "B.JPG", 5000))
    assert (what, st) == ("rapor", "farkli")                    # exif farkı: indirme yok
    what, job, st, _ = indir.plan(_job(env, "C.JPG", 5000))
    assert what == "indir" and job.path.endswith("C-5000.JPG")  # >4 KB: icloudpd gibi '-<boyut>'
    assert indir.plan(_job(env, "D.JPG", 5000))[0] == "rapor"


def test_fetch_places_file_and_records(env):
    data = b"x" * 3000
    job = _job(env, "N.JPG", len(data))
    s = FakeSession(data)
    assert indir.fetch(s, job, NOW)
    assert Path(job.path).read_bytes() == data
    assert not list((env / "lib").glob(".*.cfv-part"))
    assert (env / "st" / "indirilenler.txt").read_text().splitlines() == [job.path]


def test_fetch_size_mismatch_not_placed(env):
    job = _job(env, "S.JPG", 5000)
    assert not indir.fetch(FakeSession(b"x" * 10), job, NOW)   # sunucu kısa veriyor
    assert not Path(job.path).exists()


def test_fetch_never_overwrites_existing_target(env):
    job = _job(env, "T.JPG", 4)
    Path(job.path).parent.mkdir(parents=True)
    Path(job.path).write_bytes(b"ESKI")                         # bu arada oluşmuş
    assert not indir.fetch(FakeSession(b"YENI"), job, NOW)
    assert Path(job.path).read_bytes() == b"ESKI"


def test_fetch_full_part_links_without_request(env):
    job = _job(env, "P.JPG", 6)
    lib = Path(job.path).parent
    lib.mkdir(parents=True)
    (lib / ".P.JPG.cfv-part").write_bytes(b"tammam")            # link'ten önce ölmüş tur
    s = FakeSession(b"", status=416)
    assert indir.fetch(s, job, NOW) and s.calls == 0
    assert Path(job.path).read_bytes() == b"tammam"


def test_write_xmp_never_overwrites(env):
    m = env / "M.JPG"
    rec = {"fields": {"assetDate": {"value": 1736071200000}, "isFavorite": {"value": 1}}}
    indir.write_xmp(str(m), rec)
    assert b"<xmp:Rating>5</xmp:Rating>" in (env / "M.JPG.xmp").read_bytes()
    (env / "M.JPG.xmp").write_bytes(b"ESKI")
    indir.write_xmp(str(m), rec)
    assert (env / "M.JPG.xmp").read_bytes() == b"ESKI"
