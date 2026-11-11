#!/usr/bin/env python3
"""brain: write path, consolidator, doctor and harness adapters for the second-brain repo.

  brain add          write one entry to inbox/ (append-only, one file per entry)
  brain inbox        list pending / invalid / consolidated entries
  brain consolidate  inbox/ -> notes/log/ (validate, redact, dedupe). The only writer of notes/log/.
  brain doctor       health report; --hook prints a short context block for a SessionStart hook
  brain skills       link the skills listed in skills/ACTIVE into ~/.claude/skills
  brain adapters     install the generated harness files (AGENTS.md, GEMINI.md) as symlinks for Claude Code, Codex,
                     OpenCode, Gemini CLI and Antigravity
  brain selftest     run the built-in tests in a temporary repo
  brain asset        binary files outside git, each with a text card in notes/files/ (brain_assets.py)

Standard library only, Python 3.8+. Entries pass the extractor's redactor before they are written, so a secret that an
agent pastes into a note never reaches git. The fixed note format is `Date · Context · Decision/Result · Evidence ·
Next action`.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

KINDS = ("decision", "fact", "preference", "event", "lesson", "todo")
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
LABEL_RE = re.compile(r"^(Context|Decision/Result|Decision|Result|Evidence|Next action|Next):[ \t]*(.*)$", re.I)
LABELS = {"context": "context", "decision/result": "result", "decision": "result", "result": "result",
          "evidence": "evidence", "next action": "next", "next": "next"}
FM_ORDER = ("kind", "topic", "title", "date", "source", "host", "session")
MAX_FIELD = 4000
PENDING_WARN_HOURS = 72
VPS_STALE_HOURS = 36
OK, WARN, FAIL = "OK", "WARN", "FAIL"


# ---------------------------------------------------------------- helpers

def repo_root() -> Path:
    return Path(os.environ.get("BRAIN_REPO") or Path(__file__).resolve().parents[2])


def host() -> str:
    return re.sub(r"[^a-z0-9-]", "", socket.gethostname().split(".")[0].lower()) or "host"


def detect_source() -> str:
    if os.environ.get("BRAIN_SOURCE"):
        return re.sub(r"[^a-z0-9-]", "", os.environ["BRAIN_SOURCE"].lower()) or "agent"
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude-code"
    if any(k.startswith("CODEX_") for k in os.environ):
        return "codex"
    if any(k.startswith("OPENCODE") for k in os.environ):
        return "opencode"
    return "manual"


def load_redactor(root: Path):
    p = Path(__file__).resolve().parents[1] / "extract" / "brain_extract.py"
    spec = importlib.util.spec_from_file_location("brain_extract", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.redact


def git(root: Path, *args, check=True, timeout=120):
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r


def git_identity_args(root: Path, name: str):
    have = git(root, "config", "user.name", check=False).stdout.strip()
    return [] if have else ["-c", f"user.name={name}", "-c", f"user.email={name}@{host()}"]


def slugify(s: str, n=40) -> str:
    fold = str.maketrans({"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u", "İ": "i", "Ç": "c", "Ğ": "g",
                          "Ö": "o", "Ş": "s", "Ü": "u"})
    s = s.translate(fold).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s[:n].strip("-")) or "entry"


def one_line(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# ---------------------------------------------------------------- entries

class Invalid(Exception):
    pass


def validate(e: dict) -> dict:
    out = {k: (e.get(k) or "").strip() for k in
           ("kind", "topic", "title", "date", "source", "host", "session", "context", "result", "evidence", "next")}
    for k in ("kind", "topic", "title", "date", "source", "host", "session"):
        out[k] = one_line(out[k])
    out["session"] = re.sub(r"[^A-Za-z0-9_-]", "", out["session"])[:40]
    if out["kind"] not in KINDS:
        raise Invalid(f"kind must be one of {', '.join(KINDS)}")
    if not TOPIC_RE.match(out["topic"]):
        raise Invalid("topic must be a slug: a-z 0-9 and '-', up to 40 chars (e.g. bilun, safa, bist-quant)")
    if not 3 <= len(out["title"]) <= 120:
        raise Invalid("title must be 3-120 characters")
    try:
        dt.date.fromisoformat(out["date"])
    except ValueError:
        raise Invalid("date must be YYYY-MM-DD")
    for k, label in (("context", "Context"), ("result", "Decision/Result")):
        if len(out[k]) < 3:
            raise Invalid(f"{label} is required")
    for k in ("context", "result", "evidence", "next"):
        if len(out[k]) > MAX_FIELD:
            raise Invalid(f"{k} longer than {MAX_FIELD} characters; split the entry")
    out["evidence"] = out["evidence"] or "none"
    out["next"] = out["next"] or "none"
    return out


def entry_id(e: dict) -> str:
    norm = " | ".join(re.sub(r"\s+", " ", e[k]).strip().lower()
                      for k in ("kind", "topic", "title", "date", "context", "result"))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]


def render_inbox(e: dict) -> str:
    fm = "\n".join(f"{k}: {json.dumps(e[k], ensure_ascii=False) if k == 'title' else e[k]}"
                   for k in FM_ORDER if e.get(k))
    body = (f"Context: {e['context']}\nDecision/Result: {e['result']}\n"
            f"Evidence: {e['evidence']}\nNext action: {e['next']}\n")
    return f"---\n{fm}\n---\n{body}"


def parse_entry(text: str) -> dict:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise Invalid("missing frontmatter")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise Invalid("unterminated frontmatter")
    e = {}
    for ln in lines[1:end]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            v = v.strip()
            if v.startswith('"'):
                try:
                    v = json.loads(v)
                except ValueError:
                    v = v.strip('"')
            e[k.strip().lower()] = v
    cur = None
    for ln in lines[end + 1:]:
        m = LABEL_RE.match(ln)
        if m:
            cur = LABELS[m.group(1).lower()]
            e[cur] = m.group(2)
        elif cur and ln.strip():
            e[cur] += "\n" + ln.strip()
    return validate(e)


def redact_entry(e: dict, redact) -> int:
    total = 0
    for k in ("title", "context", "result", "evidence", "next"):
        clean, n = redact(e[k])
        e[k] = clean
        total += n
    return total


def log_path(e: dict) -> Path:
    return Path("notes") / "log" / e["date"][:4] / f"{e['date']}-{e['topic']}-{slugify(e['title'])}-{entry_id(e)[:6]}.md"


def render_log(e: dict) -> str:
    fm = ["---", "type: note", f"title: {json.dumps(e['title'], ensure_ascii=False)}", f"kind: {e['kind']}", f"topic: {e['topic']}",
          f"date: {e['date']}", f"source: {e['source'] or 'manual'}", f"host: {e['host'] or 'unknown'}"]
    if e.get("session"):
        fm.append(f"session: {e['session']}")
    fm += [f"id: {entry_id(e)}", "---", ""]
    body = [f"# {e['title']}", "",
            f"- **Date:** {e['date']} · **Topic:** {e['topic']} · **Kind:** {e['kind']}",
            f"- **Context:** {e['context']}", f"- **Decision/Result:** {e['result']}",
            f"- **Evidence:** {e['evidence']}", f"- **Next action:** {e['next']}", ""]
    return "\n".join(fm + body)


def pending_files(root: Path):
    d = root / "inbox"
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.md") if p.name.lower() != "readme.md")


def existing_ids(root: Path) -> set:
    ids = set()
    base = root / "notes" / "log"
    if base.is_dir():
        for p in base.rglob("*.md"):
            try:
                head = p.read_text(encoding="utf-8")[:600]
            except OSError:
                continue
            m = re.search(r"^id:\s*([0-9a-f]{10})\s*$", head, re.M)
            if m:
                ids.add(m.group(1))
    return ids


# ---------------------------------------------------------------- add

def cmd_add(a, root: Path) -> int:
    e = {"kind": a.kind, "topic": a.topic, "title": a.title, "date": a.date or dt.date.today().isoformat(),
         "source": a.source or detect_source(), "host": host(), "session": a.session or "",
         "context": a.context, "result": a.result, "evidence": a.evidence or "", "next": a.next or ""}
    try:
        e = validate(e)
    except Invalid as ex:
        print(f"brain add: {ex}", file=sys.stderr)
        return 2
    n = redact_entry(e, load_redactor(root))
    eid = entry_id(e)
    if eid in existing_ids(root):
        print(f"brain add: already consolidated (id {eid}); nothing written")
        return 0
    inbox = root / "inbox"
    inbox.mkdir(exist_ok=True)
    for p in pending_files(root):
        try:
            if entry_id(parse_entry(p.read_text(encoding="utf-8"))) == eid:
                print(f"brain add: identical entry already pending ({p.name}); nothing written")
                return 0
        except (Invalid, OSError):
            continue
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = inbox / f"{stamp}-{e['host']}-{slugify(e['source'], 20)}-{eid[:6]}.md"
    with open(path, "x", encoding="utf-8") as f:
        f.write(render_inbox(e))
    print(f"brain add: wrote {path.relative_to(root)} (id {eid}); consolidated into notes/log/ by the nightly run")
    if n:
        print(f"brain add: WARNING {n} secret-shaped string(s) were redacted. Never put credentials in the brain.",
              file=sys.stderr)
    return 0


# ---------------------------------------------------------------- inbox

def cmd_inbox(a, root: Path) -> int:
    pend, bad = [], []
    for p in pending_files(root):
        try:
            e = parse_entry(p.read_text(encoding="utf-8"))
            pend.append((p, e))
        except (Invalid, OSError) as ex:
            bad.append((p, str(ex)))
    done = sorted((root / "inbox" / "_done").rglob("*.md")) if (root / "inbox" / "_done").is_dir() else []
    print(f"pending {len(pend)} · invalid {len(bad)} · consolidated {len(done)}")
    for p, e in pend:
        print(f"  {e['date']} {e['topic']:<12} {e['kind']:<10} {e['title']}")
    for p, why in bad:
        print(f"  INVALID {p.name}: {why}")
    return 0


# ---------------------------------------------------------------- consolidate

def publish(root: Path, remote: str, branch: str = "main") -> bool:
    for _ in range(3):
        if git(root, "push", "-q", remote, f"HEAD:{branch}", check=False, timeout=180).returncode == 0:
            return True
        r = git(root, "pull", "-q", "--rebase", remote, branch, check=False, timeout=180)
        if r.returncode != 0:
            git(root, "rebase", "--abort", check=False)
            return False
    return False


def cmd_consolidate(a, root: Path) -> int:
    lockdir = root / ".git" if (root / ".git").is_dir() else root
    lock = open(lockdir / "brain-consolidate.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("brain consolidate: another run holds the lock; skipping")
        return 0
    redact = load_redactor(root)
    seen = existing_ids(root)
    wrote = dup = 0
    invalid = []
    for p in pending_files(root):
        try:
            e = parse_entry(p.read_text(encoding="utf-8"))
        except (Invalid, OSError) as ex:
            invalid.append((p.name, str(ex)))
            continue
        redact_entry(e, redact)
        eid = entry_id(e)
        if eid in seen:
            dup += 1
            status = "duplicate"
        else:
            target = root / log_path(e)
            if not a.dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(".tmp")
                tmp.write_text(render_log(e), encoding="utf-8")
                os.replace(tmp, target)
            seen.add(eid)
            wrote += 1
            status = f"-> {log_path(e)}"
        if not a.dry_run:
            dest = root / "inbox" / "_done" / e["date"][:7]
            dest.mkdir(parents=True, exist_ok=True)
            os.replace(p, dest / p.name)
        print(f"  {p.name}: {status}")
    print(f"consolidated {wrote} · duplicates {dup} · invalid left in inbox {len(invalid)}")
    for name, why in invalid:
        print(f"  INVALID {name}: {why}")
    if a.dry_run or not (a.commit or a.push) or (wrote + dup == 0):
        return 0
    ident = git_identity_args(root, "brain-consolidator")
    git(root, "add", "-A", "inbox", "notes/log")
    if git(root, "diff", "--cached", "--quiet", check=False).returncode != 0:
        git(root, *ident, "commit", "-q", "-m", f"consolidate: {wrote} entries ({dup} duplicates)")
        print("committed")
    if a.push:
        ok = publish(root, a.remote)
        print("pushed" if ok else "WARN: push failed (will retry on the next run)")
        return 0 if ok else 1
    return 0


# ---------------------------------------------------------------- doctor

def run_compile_check(root: Path):
    r = subprocess.run([sys.executable, "-I", str(root / "tools" / "compile" / "compile_core.py"), "--check",
                        "--root", str(root)], capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr).strip().splitlines()[-1:] or [""]


def age_h(ts: float) -> float:
    return (dt.datetime.now().timestamp() - ts) / 3600.0


def human_age(h: float) -> str:
    return f"{int(h * 60)}m" if h < 1 else (f"{h:.0f}h" if h < 48 else f"{h / 24:.0f}d")


def adapter_targets(root: Path):
    home = Path.home()
    src = root / "core" / "adapters"
    return [("claude-code", home / ".claude" / "CLAUDE.md", src / "AGENTS.md"),   # reads CLAUDE.md, not AGENTS.md
            ("codex", home / ".codex" / "AGENTS.md", src / "AGENTS.md"),
            ("opencode", home / ".config" / "opencode" / "AGENTS.md", src / "AGENTS.md"),
            ("gemini-cli", home / ".gemini" / "GEMINI.md", src / "GEMINI.md"),
            ("antigravity", home / ".gemini" / "config" / "GEMINI.md", src / "GEMINI.md")]


def adapter_state(target: Path, source: Path) -> str:
    if target.is_symlink():
        return "ok" if target.resolve() == source.resolve() else "foreign-link"
    if not target.exists():
        return "missing"
    return "empty" if target.stat().st_size == 0 else "foreign"


def mcp_argv():
    """Read-only recall server on the VPS (ops/gbrain/serve-ro.*): remember/forget are refused server-side."""
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
            "-p", os.environ.get("BRAIN_VPS_PORT", "8443"), os.environ.get("BRAIN_VPS", "root@vps"),
            "/root/gbrain/serve-ro.sh"]


def backup_file(path: Path) -> Path:
    d = Path.home() / ".cache" / "brain" / "backups"      # configs can hold secrets: 0700 dir, 0600 copies
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    dst = d / f"{path.name}.{dt.datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(path, dst)
    os.chmod(dst, 0o600)
    return dst


def strip_jsonc(text: str) -> str:
    out, i, n, in_s, esc = [], 0, len(text), False, False
    while i < n:
        c = text[i]
        if in_s:
            out.append(c)
            esc = (not esc) and c == "\\"
            if c == '"' and not esc:
                in_s = False
            if c != "\\":
                esc = False
        elif c == '"':
            in_s = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def mcp_codex(install: bool) -> str:
    cfg = Path.home() / ".codex" / "config.toml"
    if not cfg.exists():
        return "n/a"
    if re.search(r"^\[mcp_servers\.gbrain\]", cfg.read_text(encoding="utf-8"), re.M):
        return "ok"
    if not install:
        return "missing"
    codex = shutil.which("codex") or str(Path.home() / ".local" / "bin" / "codex")
    bak = backup_file(cfg)
    r = subprocess.run([codex, "mcp", "add", "gbrain", "--", *mcp_argv()], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        shutil.copy2(bak, cfg)
        return f"FAILED ({r.stderr.strip()[:80]}); config restored"
    return f"added (backup {bak.name})"


def mcp_opencode(install: bool) -> str:
    cfg = Path.home() / ".config" / "opencode" / "opencode.jsonc"
    if not cfg.exists():
        return "n/a"
    txt = cfg.read_text(encoding="utf-8")
    if re.search(r'"gbrain"\s*:\s*\{', txt):
        return "ok"
    if not install:
        return "missing"
    m = re.search(r'"mcp"\s*:\s*\{(\s*)"', txt)
    if not m:
        return "manual: add an entry to the existing non-empty \"mcp\" block (not auto-editable)"
    entry = json.dumps({"type": "local", "command": mcp_argv(), "enabled": True}, indent=2)
    entry = entry.replace("\n", "\n    ")
    new = txt[:m.start(1)] + '\n    "gbrain": ' + entry + "," + txt[m.start(1):]
    try:
        json.loads(strip_jsonc(new))
    except ValueError as ex:
        return f"FAILED: edit would not parse ({ex}); file untouched"
    bak = backup_file(cfg)
    tmp = cfg.with_suffix(".jsonc.tmp")
    tmp.write_text(new, encoding="utf-8")
    os.replace(tmp, cfg)
    return f"added (backup {bak.name})"


def mcp_antigravity(install: bool) -> str:
    cfg = Path.home() / ".gemini" / "config" / "mcp_config.json"
    if not (Path.home() / ".gemini").is_dir():
        return "n/a"
    txt = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    try:
        d = json.loads(txt) if txt.strip() else {}
    except ValueError:
        return "FAILED: existing mcp_config.json is not valid JSON; left untouched"
    if "gbrain" in (d.get("mcpServers") or {}):
        return "ok"
    if not install:
        return "missing"
    bak = backup_file(cfg) if txt.strip() else None
    d.setdefault("mcpServers", {})["gbrain"] = {"command": mcp_argv()[0], "args": mcp_argv()[1:]}
    cfg.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, cfg)
    return "added" + (f" (backup {bak.name})" if bak else "")


def mcp_muse(install: bool) -> str:
    """Muse Code: flat settings.json with a top-level mcpServers map (stdio entries need type, command, args)."""
    cfg = Path.home() / ".config" / "muse" / "settings.json"
    if not cfg.exists():
        return "n/a"
    try:
        d = json.loads(cfg.read_text(encoding="utf-8"))
    except ValueError:
        return "FAILED: existing settings.json is not valid JSON; left untouched"
    if "gbrain" in (d.get("mcpServers") or {}):
        return "ok"
    if not install:
        return "missing"
    bak = backup_file(cfg)
    d.setdefault("mcpServers", {})["gbrain"] = {"type": "stdio", "command": mcp_argv()[0], "args": mcp_argv()[1:],
                                                "mode": "optional"}
    tmp = cfg.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, cfg)
    return f"added (backup {bak.name})"


MCP_WIRING = (("codex", mcp_codex), ("opencode", mcp_opencode), ("antigravity", mcp_antigravity), ("muse", mcp_muse))


def active_skills(root: Path):
    f = root / "skills" / "ACTIVE"
    if not f.exists():
        return []
    return [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]


def skills_dir() -> Path:
    return Path(os.environ.get("BRAIN_SKILLS_DIR") or Path.home() / ".claude" / "skills")


def vps_cache_path() -> Path:
    return Path(os.environ.get("BRAIN_CACHE") or Path.home() / ".cache" / "brain") / "vps-status.json"


def doctor_checks(root: Path):
    c = []

    def add(area, level, detail, fix=""):
        c.append({"area": area, "level": level, "detail": detail, "fix": fix})

    ok, last = run_compile_check(root)
    add("digest", OK if ok else WARN, "fresh" if ok else "core/digest.md is stale vs core/src",
        "" if ok else "python3 tools/compile/compile_core.py")

    # repo
    tracked = [ln for ln in git(root, "status", "--porcelain", "--untracked-files=no", check=False).stdout.splitlines()]
    if tracked:
        add("repo", WARN, f"{len(tracked)} uncommitted tracked change(s)", "commit or stash; brain-sync stashes it for the rebase and puts it back")
    remotes = git(root, "remote", check=False).stdout.split()
    laptop = "hub" in remotes          # the VPS clone has only `origin` (the hub itself)
    synced = []
    for rn in ("hub", "origin"):
        if rn not in remotes:
            continue
        if git(root, "rev-parse", "--verify", "-q", f"{rn}/main", check=False).returncode != 0:
            continue
        lr = git(root, "rev-list", "--left-right", "--count", f"HEAD...{rn}/main", check=False).stdout.split()
        ahead, behind = (int(lr[0]), int(lr[1])) if len(lr) == 2 else (0, 0)
        if ahead or behind:
            add("repo", WARN, f"{rn}: {ahead} ahead, {behind} behind", "ops/laptop/brain-sync.sh")
        else:
            synced.append(rn)
    if synced and not tracked:
        add("repo", OK, "in sync with " + ", ".join(synced))
    fh = root / ".git" / "FETCH_HEAD"
    if laptop and fh.exists() and age_h(fh.stat().st_mtime) > 24:
        add("repo", WARN, f"last fetch {human_age(age_h(fh.stat().st_mtime))} ago", "brain-sync timer not running?")

    # inbox
    pend, bad = [], 0
    for p in pending_files(root):
        try:
            pend.append((p, parse_entry(p.read_text(encoding="utf-8"))))
        except (Invalid, OSError):
            bad += 1
    if bad:
        add("inbox", FAIL, f"{bad} invalid entr{'y' if bad == 1 else 'ies'} stuck in inbox/", "brain inbox")
    if pend:
        oldest = min(p.stat().st_mtime for p, _ in pend)
        lvl = WARN if age_h(oldest) > PENDING_WARN_HOURS else OK
        add("inbox", lvl, f"{len(pend)} pending, oldest {human_age(age_h(oldest))}",
            "consolidator runs in the VPS nightly job" if lvl == WARN else "")
    elif not bad:
        add("inbox", OK, "0 pending")

    # archive
    arch = Path(os.environ.get("BRAIN_ARCHIVE") or Path.home() / "second-brain-archive")
    if (arch / ".git").is_dir():
        dirty = git(arch, "status", "--porcelain", check=False).stdout.splitlines()
        lr = git(arch, "rev-list", "--left-right", "--count", "HEAD...origin/main", check=False).stdout.split()
        ahead, behind = (int(lr[0]), int(lr[1])) if len(lr) == 2 else (0, 0)
        ts = git(arch, "log", "-1", "--format=%ct", check=False).stdout.strip()
        age = age_h(float(ts)) if ts else 9999
        if dirty or ahead:
            add("archive", WARN, f"{len(dirty)} uncommitted, {ahead} unpushed", "ops/laptop/extract-and-push.sh")
        elif age > 72:
            add("archive", WARN, f"last extraction commit {human_age(age)} ago", "extractor timer not running?")
        else:
            add("archive", OK, f"synced, last commit {human_age(age)} ago")

    # timers (laptop only)
    if laptop and shutil.which("systemctl") and os.environ.get("XDG_RUNTIME_DIR"):
        bad_t = []
        for t in ("brain-extract.timer", "brain-sync.timer", "brain-distill.timer"):
            r = subprocess.run(["systemctl", "--user", "is-active", t], capture_output=True, text=True)
            if r.stdout.strip() not in ("active",) and "not-found" not in r.stdout + r.stderr \
                    and "Failed to connect" not in r.stderr and r.returncode not in (4,):
                bad_t.append(t)
        if bad_t:
            add("timers", WARN, "inactive: " + ", ".join(bad_t), "systemctl --user enable --now <timer>")

    # vps status cache
    cache = vps_cache_path()
    if laptop:
        if not cache.exists():
            add("vps", WARN, "no cached VPS status yet", "ops/laptop/brain-sync.sh")
        else:
            try:
                d = json.loads(cache.read_text(encoding="utf-8"))
                jobs = d.get("jobs", {})
                m = jobs.get("gbrain-maint")
                fetched = dt.datetime.fromisoformat(d["fetched"]).timestamp()
                if age_h(fetched) > 6:
                    add("vps", WARN, f"status cache is {human_age(age_h(fetched))} old", "brain-sync timer not running?")
                if not m:
                    add("vps", WARN, "gbrain-maint status unreadable on the VPS", "ssh root@vps cat /root/gbrain/status/gbrain-maint.json")
                else:
                    fin = dt.datetime.fromisoformat(m["finished"])
                    when = fin.strftime("%m-%d %H:%M")
                    problems = []
                    if m.get("result") != "success":
                        problems.append(f"result={m.get('result')}")
                    if m.get("failed_phases"):
                        problems.append("failed phase: " + m["failed_phases"][:70])
                    if m.get("warnings"):
                        problems.append("warn: " + m["warnings"][:70])
                    if age_h(fin.timestamp()) > VPS_STALE_HOURS:
                        problems.append(f"last run {human_age(age_h(fin.timestamp()))} ago")
                    add("vps", WARN if problems else OK, f"nightly {m.get('result')} {when}" + (
                        " — " + "; ".join(problems) if problems else ""), "journalctl -u gbrain-maint -n 50" if problems else "")
            except (ValueError, KeyError, OSError) as ex:
                add("vps", WARN, f"status cache unreadable ({ex.__class__.__name__})", "ops/laptop/brain-sync.sh")

    # adapters: anything but the link to the generated file is "not wired" (a hand-made stub will not follow the digest)
    states = [(n, adapter_state(t, s)) for n, t, s in adapter_targets(root) if s.exists() and t.parent.is_dir()]
    miss = [n for n, st in states if st != "ok"]
    miss += [f"{n} mcp" for n, fn in MCP_WIRING if fn(False) == "missing"]
    if laptop and (root / "core" / "adapters").is_dir():
        fix = "brain adapters --install" + (" --force" if any(st == "foreign" for _, st in states) else "")
        add("adapters", WARN if miss else OK, ("not wired: " + ", ".join(miss)) if miss else "wired",
            fix if miss else "")

    # skills
    want = active_skills(root)
    if laptop and want:
        sd = skills_dir()
        missing = [w for w in want if not (sd / Path(w).name).exists()]
        add("skills", WARN if missing else OK,
            (f"{len(missing)} of {len(want)} active skills not linked") if missing else f"{len(want)} active",
            "brain skills sync" if missing else "")
    return c


def cmd_doctor(a, root: Path) -> int:
    checks = doctor_checks(root)
    worst = FAIL if any(c["level"] == FAIL for c in checks) else (WARN if any(c["level"] == WARN for c in checks) else OK)
    if a.json:
        print(json.dumps({"status": worst, "checks": checks}, ensure_ascii=False, indent=1))
        return 0 if worst == OK else 1
    pend = []
    for p in pending_files(root):
        try:
            pend.append(parse_entry(p.read_text(encoding="utf-8")))
        except (Invalid, OSError):
            pass
    if a.hook:
        if worst == OK:
            line = " · ".join(f"{c['area']} {c['detail']}" for c in checks
                              if c["area"] in ("digest", "repo", "inbox", "vps", "archive", "skills"))
            print(f"brain: ok — {line}")
        else:
            bad = [c for c in checks if c["level"] != OK]
            print(f"brain doctor: {len(bad)} issue(s)")
            for c in bad:
                print(f"- [{c['level']}] {c['area']}: {c['detail']}" + (f" → {c['fix']}" if c["fix"] else ""))
        for e in pend[:5]:
            print(f"- pending in inbox: {e['date']} {e['topic']} · {e['title']}")
        if len(pend) > 5:
            print(f"- … and {len(pend) - 5} more pending")
        return 0
    for c in checks:
        print(f"{c['level']:<4} {c['area']:<9} {c['detail']}" + (f"   → {c['fix']}" if c["fix"] else ""))
    return 0 if worst == OK else 1


def cmd_vps_cache(a, root: Path) -> int:
    """Fetch the nightly status records from the VPS into the cache that `doctor` reads (no network in doctor)."""
    target = os.environ.get("BRAIN_VPS", "root@vps")
    port = os.environ.get("BRAIN_VPS_PORT", "8443")
    cmd = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-p", port, target,
           "for j in gbrain-maint gbrain-vault-sync; do printf '%s\\t' $j; cat /root/gbrain/status/$j.json 2>/dev/null || echo '{}'; echo; done"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        print("vps-cache: ssh timed out", file=sys.stderr)
        return 1
    if r.returncode != 0:
        print(f"vps-cache: ssh failed ({r.returncode})", file=sys.stderr)
        return 1
    jobs = {}
    for ln in r.stdout.splitlines():
        if "\t" in ln:
            name, js = ln.split("\t", 1)
            try:
                d = json.loads(js)
                if d:
                    jobs[name] = d
            except ValueError:
                pass
    p = vps_cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fetched": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "jobs": jobs}))
    os.replace(tmp, p)
    return 0


# ---------------------------------------------------------------- skills + adapters

def link(target: Path, source: Path, force=False) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        if target.resolve() == source.resolve():
            return "ok"
        target.unlink()
    elif target.exists():
        if target.is_file() and target.stat().st_size == 0:
            target.unlink()
        elif force:
            bak = target.with_name(target.name + f".bak-{dt.date.today():%Y%m%d}")
            target.rename(bak)
        else:
            return "skipped (exists with other content; --force backs it up)"
    target.symlink_to(source)
    return "linked"


def cmd_skills(a, root: Path) -> int:
    sd = skills_dir()
    want = active_skills(root)
    names = [Path(w).name for w in want]
    if len(set(names)) != len(names):
        print("brain skills: duplicate skill names in skills/ACTIVE", file=sys.stderr)
        return 2
    if a.action == "list":
        for w in want:
            st = "linked" if (sd / Path(w).name).is_symlink() else "NOT linked"
            print(f"{st:<11} {w}")
        print(f"{len(want)} active; the rest of skills/INDEX.md stays on disk, off the always-loaded list")
        return 0
    rc = 0
    for w in want:
        src = root / "skills" / w
        if not (src / "SKILL.md").is_file():
            print(f"MISSING     {w}")
            rc = 1
            continue
        print(f"{link(sd / Path(w).name, src):<11} {w}")
    # drop links we own that are no longer active
    if sd.is_dir():
        for p in sd.iterdir():
            if p.is_symlink() and str(p.resolve()).startswith(str(root / "skills")) and p.name not in names:
                p.unlink()
                print(f"removed     {p.name}")
    return rc


def cmd_adapters(a, root: Path) -> int:
    rc = 0
    print("instructions (always-on digest):")
    for name, target, source in adapter_targets(root):
        if not source.exists():
            print(f"  {name:<12} source missing: {source} (run tools/compile/compile_core.py)")
            rc = 1
            continue
        if a.install:
            print(f"  {name:<12} {link(target, source, force=a.force)}  {target}")
        else:
            print(f"  {name:<12} {adapter_state(target, source):<12} {target}")
    print("recall tier (gbrain MCP, read-only entry point):")
    for name, fn in MCP_WIRING:
        st = fn(a.install)
        print(f"  {name:<12} {st}")
        if st.startswith(("FAILED", "manual")):
            rc = 1
    return rc


# ---------------------------------------------------------------- selftest

def cmd_selftest(a, root: Path) -> int:
    real = repo_root()
    ok = 0

    def check(cond, label):
        nonlocal ok
        if not cond:
            raise AssertionError(label)
        ok += 1

    with tempfile.TemporaryDirectory() as td:
        t = Path(td) / "repo"
        (t / "tools" / "extract").mkdir(parents=True)
        shutil.copy(real / "tools" / "extract" / "brain_extract.py", t / "tools" / "extract" / "brain_extract.py")
        (t / "tools" / "compile").mkdir(parents=True)
        shutil.copy(real / "tools" / "compile" / "compile_core.py", t / "tools" / "compile" / "compile_core.py")
        subprocess.run(["git", "init", "-q", "-b", "main", str(t)], check=True)
        ns = argparse.Namespace
        base = dict(kind="decision", topic="bilun", title="Gate Aliyun plan on a quota probe", date="2026-10-13",
                    source="claude-code", session="", context="Plan renewal was due.", result="Renew only after a probe.",
                    evidence="session 8feea9c6, commit 6e43bd1a2b3c4d5e", next="")
        check(cmd_add(ns(**base), t) == 0, "add ok")
        check(len(pending_files(t)) == 1, "one pending")
        check(cmd_add(ns(**base), t) == 0 and len(pending_files(t)) == 1, "identical pending entry is not written twice")
        check("commit 6e43bd1a2b3c4d5e" in pending_files(t)[0].read_text(), "commit hashes survive redaction")
        secret = dict(base, title="Secret probe entry", result="key is sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789ABCD end")
        cmd_add(ns(**secret), t)
        txt = "\n".join(p.read_text() for p in pending_files(t))
        check("sk-ant-api03-abcdefghijklmnopqrstuvwxyz" not in txt, "secret never reaches the inbox")
        check(cmd_add(ns(**dict(base, kind="rumor")), t) == 2, "bad kind rejected")
        check(cmd_add(ns(**dict(base, topic="Bad Topic")), t) == 2, "bad topic rejected")
        (t / "inbox" / "20261013T000000Z-hand-written-000000.md").write_text("no frontmatter here\n")
        check(main(["consolidate", "--repo", str(t), "--dry-run"]) == 0, "--repo after the subcommand parses")
        check(main(["--repo", str(t), "inbox"]) == 0, "--repo before the subcommand parses")
        r = cmd_consolidate(ns(dry_run=False, commit=False, push=False, remote="origin"), t)
        logs = sorted((t / "notes" / "log").rglob("*.md"))
        check(r == 0 and len(logs) == 2, "two entries consolidated")
        check(len(pending_files(t)) == 1, "invalid entry stays in inbox")
        check("Gate Aliyun plan" in logs[0].read_text() + logs[1].read_text(), "log has the title")
        check(all(re.search(r"^id: [0-9a-f]{10}$", p.read_text(), re.M) for p in logs), "log files carry ids")
        check(any("2026-10-13-bilun-gate-aliyun-plan-on-a-quota-probe-" in p.name for p in logs), "slugged file name")
        # idempotent + dedupe across a fresh inbox file
        cmd_consolidate(ns(dry_run=False, commit=False, push=False, remote="origin"), t)
        check(len(list((t / "notes" / "log").rglob("*.md"))) == 2, "re-run is a no-op")
        check(cmd_add(ns(**base), t) == 0, "re-add of consolidated entry is a no-op")
        check(len(pending_files(t)) == 1, "nothing new pending after re-add")
        check(slugify("Çok önemli İş şüphesi") == "cok-onemli-is-suphesi", "turkish fold")
        # doctor runs
        os.environ["BRAIN_ARCHIVE"] = str(Path(td) / "no-archive")
        checks = doctor_checks(t)
        check(any(c["area"] == "inbox" and c["level"] == FAIL for c in checks), "doctor flags the invalid entry")
        # adapters: Claude Code is a target like the others; a hand-made stub is reported and --force replaces it
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(Path(td) / "home")
        try:
            check(adapter_targets(t)[0][0] == "claude-code", "claude-code is an adapter target")
            src = t / "core" / "adapters" / "AGENTS.md"
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text("digest\n", encoding="utf-8")
            cc = Path(td) / "home" / ".claude" / "CLAUDE.md"
            cc.parent.mkdir(parents=True)
            check(adapter_state(cc, src) == "missing", "an unwired target is missing")
            cc.write_text("@~/second-brain/core/digest.md\n", encoding="utf-8")
            check(adapter_state(cc, src) == "foreign", "a hand-made stub is foreign")
            check(link(cc, src).startswith("skipped"), "link does not replace a stub without --force")
            check(link(cc, src, force=True) == "linked" and adapter_state(cc, src) == "ok", "--force links it")
            check(any(p.name.startswith("CLAUDE.md.bak-") for p in cc.parent.iterdir()), "the stub is kept as .bak")
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home
    with tempfile.TemporaryDirectory() as td:
        ok += assets_module().selftest(Path(td))
    print(f"selftest ok ({ok} checks)")
    return 0


# ---------------------------------------------------------------- assets

def assets_module():
    sys.path.insert(0, str(Path(__file__).resolve().parent))   # also under python -I, which drops the script's folder
    import brain_assets
    return brain_assets


def cmd_asset(a, root: Path) -> int:
    redact = load_redactor(root)
    return assets_module().run(a, root, lambda s: redact(s)[0])


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)   # --repo is accepted before or after the subcommand
    common.add_argument("--repo", default=argparse.SUPPRESS,
                        help="repo root (default: BRAIN_REPO or the repo this script lives in)")
    ap = argparse.ArgumentParser(prog="brain", description=__doc__.split("\n")[0], parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", parents=[common], help="write an entry to inbox/")
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--topic", required=True, help="slug, e.g. bilun, safa, bist-quant")
    p.add_argument("--title", required=True)
    p.add_argument("--context", required=True, help="what was going on; one or two sentences")
    p.add_argument("--result", required=True, help="the decision / verified result / fact")
    p.add_argument("--evidence", help="where it comes from: file, command output, session, source (default: none)")
    p.add_argument("--next", help="next action (default: none)")
    p.add_argument("--date", help="YYYY-MM-DD (default: today)")
    p.add_argument("--source", help="harness name (default: detected)")
    p.add_argument("--session", help="session id prefix, if known")

    sub.add_parser("inbox", parents=[common], help="list pending, invalid and consolidated entries")

    p = sub.add_parser("consolidate", parents=[common], help="inbox/ -> notes/log/")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--commit", action="store_true")
    p.add_argument("--push", action="store_true", help="commit, then pull --rebase and push to --remote")
    p.add_argument("--remote", default="origin")

    p = sub.add_parser("doctor", parents=[common], help="health report")
    p.add_argument("--hook", action="store_true", help="short context block for a SessionStart hook; always exit 0")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("skills", parents=[common], help="link active skills into ~/.claude/skills")
    p.add_argument("action", nargs="?", default="list", choices=("list", "sync"))

    p = sub.add_parser("adapters", parents=[common], help="install or check harness adapter files")
    p.add_argument("--install", action="store_true")
    p.add_argument("--force", action="store_true", help="back up and replace a non-empty foreign file")

    sub.add_parser("vps-cache", parents=[common], help="refresh the cached VPS nightly status that doctor reads")
    sub.add_parser("selftest", parents=[common], help="run built-in tests")
    assets_module().add_parser(sub, common)

    a = ap.parse_args(argv)
    root = Path(getattr(a, "repo", None) or repo_root())
    fn = {"add": cmd_add, "inbox": cmd_inbox, "consolidate": cmd_consolidate, "doctor": cmd_doctor,
          "skills": cmd_skills, "adapters": cmd_adapters, "vps-cache": cmd_vps_cache, "selftest": cmd_selftest,
          "asset": cmd_asset}[a.cmd]
    try:
        return fn(a, root)
    except AssertionError as ex:
        print(f"selftest FAILED: {ex}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
