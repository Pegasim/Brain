"""brain asset: binary files beside the text brain.

Originals live outside git in a store sorted by topic (~/brain-assets/<topic>/<name>.<ext>) with a plain rsync copy
on the VPS (unencrypted, by Safa's choice; root-only folders). Each file gets a small text card in notes/files/, which
the normal index searches. The card keeps the file's hash and its path inside the store. Search finds the card;
`brain asset get` gives the path so a model can look at the file itself.

A card with no `path:` line belongs to the old layout (~/brain-assets/sha256/ab/<hash>.<ext>), which get and verify
still read.

Text in a card marked "model-made" is never a verified fact. Standard library only, except rsync and the optional
pdftotext on PATH.
"""
import datetime as dt
import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

VPS = os.environ.get("BRAIN_VPS", "root@vps")
VPS_SSH = "ssh -o BatchMode=yes -o ConnectTimeout=15 -p 8443"
VPS_FOLDER = "/root/brain-assets/"
EXCERPT_CHARS = 2000
MEDIA = {"jpg": "image", "jpeg": "image", "png": "image", "webp": "image", "heic": "image", "gif": "image",
         "mp4": "video", "mov": "video", "mkv": "video", "webm": "video", "mp3": "audio", "m4a": "audio",
         "wav": "audio", "ogg": "audio", "pdf": "document", "docx": "document", "pptx": "document", "xlsx": "document"}


def store_dir() -> Path:
    return Path(os.environ.get("BRAIN_ASSETS") or Path.home() / "brain-assets")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_path(sha: str, ext: str) -> Path:
    """The old layout: the file named by its hash. Only for cards that have no `path:` line."""
    name = f"{sha}.{ext}" if ext else sha
    return store_dir() / "sha256" / sha[:2] / name


def card_path(card: Path) -> Path:
    """Where a card's file lives: its own `path:` inside the store, else the old hash location."""
    rel = card_field(card, "path")
    return store_dir() / rel if rel else hash_path(card_field(card, "sha256"), card_field(card, "extension"))


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower().translate(str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosucgiosu"))).strip("-")
    return s[:50].strip("-") or "asset"


def new_rel_path(topic: str, captured: str, title: str, ext: str, sha: str) -> str:
    """<topic>/<captured>-<title>.<ext>, or <topic>/<title>.<ext> when the capture date is unknown. A different file
    that already has that name gets the first 6 characters of its hash added."""
    folder = slug(topic) if topic else "unsorted"
    base = f"{captured}-{slug(title)}" if captured else slug(title)
    for tail in ("", f"-{sha[:6]}"):
        rel = f"{folder}/{base}{tail}" + (f".{ext}" if ext else "")
        taken = store_dir() / rel
        if not taken.exists() or sha256_file(taken) == sha:
            return rel
    raise ValueError(f"cannot name {sha[:12]}: {rel} already holds another file")


def put_file(src: Path, target: Path, sha: str) -> bool:
    """Copy src to target (mode 600) unless target already holds it; False when the copy does not match the hash."""
    if target.is_file() and sha256_file(target) == sha:
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    shutil.copyfile(src, tmp)
    if sha256_file(tmp) != sha:
        tmp.unlink()
        return False
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    return True


def extract_text(path: Path) -> str:
    """The first characters of a document's text; empty when there is none or no tool can read it."""
    ext = path.suffix.lower()
    text = ""
    if ext == ".pdf" and shutil.which("pdftotext"):
        try:
            text = subprocess.run(["pdftotext", "-l", "5", str(path), "-"], capture_output=True, text=True,
                                  errors="replace", timeout=120).stdout
        except (OSError, subprocess.SubprocessError):
            text = ""
    elif ext == ".docx":
        try:
            with zipfile.ZipFile(path) as z:
                xml = z.read("word/document.xml").decode("utf-8", "replace")
            text = re.sub(r"<[^>]+>", " ", xml.replace("</w:p>", "\n"))
        except (OSError, KeyError, zipfile.BadZipFile):
            text = ""
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()[:EXCERPT_CHARS]


def find_card(root: Path, ident: str):
    """The card whose hash starts with ident (at least 8 characters)."""
    if len(ident) < 8 or not re.fullmatch(r"[0-9a-f]+", ident):
        return None
    for card in sorted((root / "notes" / "files").glob("*.md")):
        if card.stem.rsplit("-", 1)[-1] in (ident[:12], ident) or card_field(card, "sha256").startswith(ident):
            return card
    return None


def card_field(card: Path, key: str) -> str:
    m = re.search(rf"^{key}: *(.*)$", card.read_text(encoding="utf-8"), re.M)
    return m.group(1).strip().strip('"') if m else ""


def q(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def cmd_add(a, root: Path, redact) -> int:
    src = Path(a.file).expanduser()
    if not src.is_file():
        print(f"brain asset: no such file: {src}", file=sys.stderr)
        return 2
    sha = sha256_file(src)
    ext = src.suffix.lower().lstrip(".")
    existing = find_card(root, sha)
    if existing is not None:
        if not put_file(src, card_path(existing), sha):
            print("brain asset: the copy does not match the source; nothing stored", file=sys.stderr)
            return 1
        print(f"brain asset: already in the brain: {existing.relative_to(root)}")
        return 0
    title = a.title or src.stem
    rel = new_rel_path(a.topic or "", a.captured or "", title, ext, sha)
    target = store_dir() / rel
    if not put_file(src, target, sha):
        print("brain asset: the copy does not match the source; nothing stored", file=sys.stderr)
        return 1
    fm = {"type": "asset", "title": q(title), "sha256": sha, "extension": ext, "path": rel,
          "media": MEDIA.get(ext, "other"), "bytes": src.stat().st_size, "topic": a.topic or "",
          "captured": a.captured or "", "added": dt.date.today().isoformat(), "source": q(a.source or src.name),
          "sensitive": "true" if a.sensitive else "false"}
    lines = ["---"] + [f"{k}: {v}" for k, v in fm.items()] + ["---", "", f"# {title}", "",
                                                              f"Original: `brain asset get {sha[:12]}`"]
    if a.note:
        lines += ["", "## Note", "", redact(a.note)]
    if a.describe:
        lines += ["", "## Description (model-made, not a verified fact)", "", redact(a.describe)]
    excerpt = "" if a.sensitive else extract_text(target)
    if excerpt:
        lines += ["", f"## Text excerpt (machine-extracted, first {EXCERPT_CHARS} characters at most)", "",
                  redact(excerpt)]
    cards = root / "notes" / "files"
    cards.mkdir(parents=True, exist_ok=True)
    card = cards / f"{slug(title)}-{sha[:12]}.md"
    card.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"brain asset: {sha[:12]} stored at {rel}, card {card.relative_to(root)}")
    return 0


def cmd_get(a, root: Path) -> int:
    card = find_card(root, a.id)
    if card is None:
        print(f"brain asset: no card for {a.id}", file=sys.stderr)
        return 2
    path = card_path(card)
    if not path.is_file():
        print(f"brain asset: not on this machine: {path}. Restore it with: brain asset restore", file=sys.stderr)
        return 1
    print(path)
    return 0


def cmd_list(a, root: Path) -> int:
    cards = sorted((root / "notes" / "files").glob("*.md"))
    for card in cards:
        print(f"{card_field(card, 'sha256')[:12]}  {card_field(card, 'media'):9} {card_field(card, 'title')}")
    print(f"{len(cards)} assets")
    return 0


def cmd_verify(a, root: Path) -> int:
    """Every card has a file on this machine, and every file still hashes to the hash in its card."""
    bad = 0
    cards = sorted((root / "notes" / "files").glob("*.md"))
    for card in cards:
        sha = card_field(card, "sha256")
        path = card_path(card)
        if not path.is_file():
            print(f"MISSING {sha[:12]} {card.name}")
            bad += 1
        elif sha256_file(path) != sha:
            print(f"DAMAGED {sha[:12]} {card.name}")
            bad += 1
    print(f"{len(cards) - bad}/{len(cards)} assets intact")
    return 1 if bad else 0


def cmd_backup(a, root: Path) -> int:
    """Copy the store to the VPS; skipped when --if-changed and nothing was added since the last copy. The copy only
    adds: nothing is ever deleted on the VPS, so a file that was renamed or removed here lingers there until it is
    cleaned up by hand."""
    store = store_dir()
    if not store.is_dir():
        print("brain asset: no store yet; nothing to back up")
        return 0
    stamp = Path.home() / ".cache" / "brain" / "assets-backup.stamp"
    state = hashlib.sha256("\n".join(sorted(str(p) for p in store.rglob("*") if p.is_file())).encode()).hexdigest()
    if a.if_changed and stamp.is_file() and stamp.read_text().strip() == state:
        return 0
    r = subprocess.run(["rsync", "-a", "--partial", "-e", VPS_SSH, f"{store}/", f"{VPS}:{VPS_FOLDER}"])
    if r.returncode != 0:
        print("brain asset: copy to the VPS failed", file=sys.stderr)
        return 1
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(state + "\n")
    print("brain asset: backup done")
    return 0


def cmd_restore(a, root: Path) -> int:
    """Copy the store back from the VPS, into the store by default or into --target."""
    target = Path(a.target).expanduser() if a.target else store_dir()
    target.mkdir(parents=True, exist_ok=True)
    return subprocess.run(["rsync", "-a", "--partial", "-e", VPS_SSH, f"{VPS}:{VPS_FOLDER}", f"{target}/"]).returncode


def run(a, root: Path, redact) -> int:
    if a.action == "add":
        return cmd_add(a, root, redact)
    return {"get": cmd_get, "list": cmd_list, "verify": cmd_verify, "backup": cmd_backup,
            "restore": cmd_restore}[a.action](a, root)


def add_parser(sub, common) -> None:
    p = sub.add_parser("asset", parents=[common], help="binary files beside the text brain")
    acts = p.add_subparsers(dest="action", required=True)
    q_ = acts.add_parser("add", parents=[common], help="store a file and write its text card")
    q_.add_argument("file")
    q_.add_argument("--title")
    q_.add_argument("--note", help="what this is and why it matters (human)")
    q_.add_argument("--describe", help="description written by a model; marked as such in the card")
    q_.add_argument("--topic", help="slug, e.g. bilun, safa; it is the folder in the store")
    q_.add_argument("--captured", help="when it was made, YYYY-MM-DD")
    q_.add_argument("--source", help="where it came from (default: the file name)")
    q_.add_argument("--sensitive", action="store_true", help="no text extraction into the card")
    q_ = acts.add_parser("get", parents=[common], help="print the local path of an asset")
    q_.add_argument("id", help="first 12 characters of its hash, from the card")
    acts.add_parser("list", parents=[common], help="list assets")
    acts.add_parser("verify", parents=[common], help="check every card's file against its hash")
    q_ = acts.add_parser("backup", parents=[common], help="copy the store to the VPS")
    q_.add_argument("--if-changed", action="store_true")
    q_ = acts.add_parser("restore", parents=[common], help="copy files back from the VPS")
    q_.add_argument("--target", help="folder to copy into (default: the store)")


def selftest(root_tmp: Path) -> int:
    """Called by brain selftest. Returns the number of checks passed; raises AssertionError on failure."""
    import argparse
    old = os.environ.get("BRAIN_ASSETS")
    os.environ["BRAIN_ASSETS"] = str(root_tmp / "store")
    identity = lambda s: s  # noqa: E731
    ok = 0
    try:
        src = root_tmp / "foto.jpg"
        src.write_bytes(b"not really a photo")
        ns = argparse.Namespace(file=str(src), title="Test foto", note="a note", describe="looks like x", topic="safa",
                                captured="2026-01-01", source=None, sensitive=True, id="")
        assert cmd_add(ns, root_tmp, identity) == 0, "asset add"
        ok += 1
        sha = sha256_file(src)
        cards = list((root_tmp / "notes" / "files").glob("*.md"))
        assert len(cards) == 1, "one card"
        path = card_path(cards[0])
        assert path == store_dir() / "safa" / "2026-01-01-test-foto.jpg", "file is stored by topic and name"
        assert path.read_bytes() == src.read_bytes(), "stored file matches the source"
        ok += 1
        assert "model-made" in cards[0].read_text(), "card marks model text"
        ok += 1
        assert cmd_add(ns, root_tmp, identity) == 0 and len(list(cards[0].parent.glob("*.md"))) == 1, "add is idempotent"
        ok += 1
        ns.id = sha[:12]
        assert cmd_get(ns, root_tmp) == 0, "get finds the file"
        ok += 1
        assert cmd_verify(ns, root_tmp) == 0, "verify passes"
        ok += 1
        other = root_tmp / "foto2.jpg"
        other.write_bytes(b"another photo with the same title")
        ns2 = argparse.Namespace(**{**vars(ns), "file": str(other)})
        assert cmd_add(ns2, root_tmp, identity) == 0, "add a second file with the same title"
        second = [c for c in (root_tmp / "notes" / "files").glob("*.md") if c != cards[0]]
        assert len(second) == 1 and card_path(second[0]) != path and card_path(second[0]).is_file(), "same name, other file"
        ok += 1
        legacy = b"a card from the old layout"
        lsha = hashlib.sha256(legacy).hexdigest()
        hash_path(lsha, "pdf").parent.mkdir(parents=True)
        hash_path(lsha, "pdf").write_bytes(legacy)
        (root_tmp / "notes" / "files" / f"old-{lsha[:12]}.md").write_text(
            f"---\ntype: asset\ntitle: \"Old\"\nsha256: {lsha}\nextension: pdf\n---\n", encoding="utf-8")
        ns.id = lsha[:12]
        assert cmd_get(ns, root_tmp) == 0 and cmd_verify(ns, root_tmp) == 0, "a card without path: still resolves"
        ok += 1
        path.write_bytes(b"damaged")
        assert cmd_verify(ns, root_tmp) == 1, "verify catches damage"
        ok += 1
    finally:
        if old is None:
            os.environ.pop("BRAIN_ASSETS", None)
        else:
            os.environ["BRAIN_ASSETS"] = old
    return ok
