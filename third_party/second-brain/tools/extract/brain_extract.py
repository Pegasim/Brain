#!/usr/bin/env python3
"""brain-extract: turn agent session traces into conversation-only markdown.

Keeps   user and assistant text, one line per tool call (tool + target), session metadata.
Drops   tool outputs, images, reasoning, injected context, event logs, sub-agent chatter.
Output  is deterministic (no extraction timestamps), so unchanged sessions never produce a git diff.
Safety  secrets and ID-shaped strings are redacted BEFORE anything is written to disk.

usage:
  brain_extract.py --selftest
  brain_extract.py --out DIR [--harness claude-code|codex|opencode|antigravity|muse|claude-web|gemini|all] [--since-days N]
                   [--claude-root P] [--codex-root P] [--opencode-db P] [--dry-run] [--limit N]
Python 3.8+, standard library only.
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import socket
import sqlite3
import sys
from html.parser import HTMLParser
from pathlib import Path

VERSION = "brain-extract/1"
HOST = socket.gethostname().lower()
MIN_USER_CHARS = 120      # sessions with less typed text than this are dropped as noise
MIN_ASSIST_CHARS = 120

# ----------------------------------------------------------------------------- redaction
R_PRIVKEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
R_URLPW = re.compile(r"(?P<s>\b[A-Za-z][A-Za-z0-9+.-]*://[^\s:/@]+:)[^\s@/]{3,}(?=@)")
R_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}")
TOKEN_RULES = [
    ("sk", re.compile(r"(?<![A-Za-z0-9])sk-(?:ant-|proj-|or-v1-)?(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{20,}")),
    ("aws", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("github", re.compile(r"\b(?:(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b")),
    ("slack", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("huggingface", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("npm", re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b")),
    ("stripe", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("telegram", re.compile(r"(?<![0-9])\d{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])")),
    ("api-key", re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{32}\.[A-Za-z0-9]{16}(?![A-Za-z0-9])")),
]
R_ASSIGN = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*(?:api[_-]?key|secret|passwd|password|pwd|access[_-]?token|auth[_-]?token|private[_-]?key)"
    r"[A-Za-z0-9_.-]*)(\s*[:=]\s*)([\"']?)([^\s\"'`,;)}\]]{8,})"
)
R_TCKN = re.compile(r"(?<!\d)[1-9]\d{10}(?!\d)")
R_IBAN = re.compile(r"\bTR\d{2}(?:\s?\d{4}){5}\s?\d{2}\b")
R_CARD = re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}\b")


def _tckn_ok(s):
    d = [int(c) for c in s]
    return ((sum(d[0:9:2]) * 7 - sum(d[1:8:2])) % 10 == d[9]) and (sum(d[:10]) % 10 == d[10])


def _own_ids():
    """Safa's own identity numbers (core/own-identifiers.txt, one per line, '#' comments): kept in the clear.
    Every other ID-shaped number, for example a patient's, is still redacted."""
    p = Path(__file__).resolve().parents[2] / "core" / "own-identifiers.txt"
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {re.sub(r"\D", "", ln.split("#")[0]) for ln in lines if ln.split("#")[0].strip()}


def _luhn_ok(s):
    ds = [int(c) for c in re.sub(r"\D", "", s)]
    t = 0
    for i, x in enumerate(reversed(ds)):
        t += x if i % 2 == 0 else (x * 2 - 9 if x * 2 > 9 else x * 2)
    return t % 10 == 0


def redact(text):
    """Return (clean_text, number_of_redactions)."""
    n = 0

    def sub(rx, repl):
        nonlocal text, n
        text, k = rx.subn(repl, text)
        n += k

    sub(R_PRIVKEY, "[REDACTED:private-key]")
    sub(R_URLPW, r"\g<s>[REDACTED]")
    sub(R_BEARER, "Bearer [REDACTED]")
    for tag, rx in TOKEN_RULES:
        sub(rx, f"[REDACTED:{tag}]")

    def assign(m):
        nonlocal n
        v = m.group(4)
        if v.startswith("[REDACTED"):
            return m.group(0)
        if re.search(r"\d", v) or len(v) >= 24:
            n += 1
            return f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED]"
        return m.group(0)

    text = R_ASSIGN.sub(assign, text)

    def tckn(m):
        nonlocal n
        if _tckn_ok(m.group()) and m.group() not in _own_ids():
            n += 1
            return "[REDACTED:tckn]"
        return m.group()

    text = R_TCKN.sub(tckn, text)
    sub(R_IBAN, "[REDACTED:iban]")

    def card(m):
        nonlocal n
        g = m.group()
        if not _luhn_ok(g):
            return g
        if all(1900 <= int(x) <= 2100 for x in re.split(r"[ -]", g)):
            return g
        if re.search(r"(?i)orcid[^0-9]{0,30}$", text[max(0, m.start() - 40):m.start()]):
            return g
        n += 1
        return "[REDACTED:card]"

    text = R_CARD.sub(card, text)
    return text, n


def selftest():
    """Fake secrets are built at runtime so this file itself never contains key-shaped literals."""
    cases = [
        ("sk", "key is sk-" + "a1b2c3d4e5" * 3, "REDACTED:sk"),
        ("aws", "AKIA" + "ABCDEFGHIJKLMNOP", "REDACTED:aws"),
        ("github", "ghp_" + "Ab1" * 12, "REDACTED:github"),
        ("jwt", "eyJ" + "a" * 12 + ".eyJ" + "b" * 12 + "." + "c" * 12, "REDACTED:jwt"),
        ("telegram", "1234567890:" + "A" * 35, "REDACTED:telegram"),
        ("api-key", "0123456789abcdef" * 2 + "." + "Zz9" * 5 + "Q", "REDACTED:api-key"),
        ("url-password", "postgresql://user:" + "hunter2hunter2" + "@127.0.0.1:5432/db", "user:[REDACTED]@"),
        ("bearer", "Authorization: Bearer " + "x" * 30, "Bearer [REDACTED]"),
        ("assign", "export OPENAI_API_KEY=" + "q1w2e3r4t5y6u7i8", "[REDACTED]"),
        ("tckn", "no " + "10000000146", "REDACTED:tckn"),
        ("iban", "IBAN TR" + "33 0006 1005 1978 6457 8413 26", "REDACTED:iban"),
        ("private-key", "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----", "REDACTED:private-key"),
    ]
    keep = [
        "risk-adjusted-outcome-measures-for-billing",
        "see https://orcid.org/0000-0002-1825-0097 for the author",
        "years 2015-2016-2017-2018 were covered",
        "commit " + "9fceb02d0ae598e95dc970b74767f19372d61724",
        "the token = approach works",
    ]
    bad = 0
    for name, s, expect in cases:
        out, n = redact(s)
        ok = n >= 1 and expect in out
        bad += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} redact {name}")
    for s in keep:
        out, n = redact(s)
        ok = n == 0 and out == s
        bad += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} keep   {s[:48]}")
    # antigravity: a record cut mid-line (and mid-character) by the 100 KB chunking must survive
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "sess" / AGY_CHUNKS
        d.mkdir(parents=True)
        recs = [
            {"type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-01-01T10:00:00Z",
             "content": "<USER_REQUEST>\nşifre testi " + "x" * 30 + "\n</USER_REQUEST>\n<ADDITIONAL_METADATA>t</ADDITIONAL_METADATA>"},
            {"type": "PLANNER_RESPONSE", "source": "MODEL", "created_at": "2026-01-01T10:00:05Z", "thinking": "hidden",
             "tool_calls": [{"name": "run_command", "args": {"CommandLine": '"ls -la"', "Cwd": '"/tmp/proj"'}}]},
            {"type": "PLANNER_RESPONSE", "source": "MODEL", "created_at": "2026-01-01T10:00:09Z", "content": "done ✓"},
            {"type": "RUN_COMMAND", "source": "MODEL", "created_at": "2026-01-01T10:00:07Z", "content": "SECRET-OUTPUT"},
        ]
        blob = "\n".join(json.dumps(r, ensure_ascii=False) for r in recs).encode("utf-8")
        cut = blob.index("ş".encode("utf-8")) + 1  # inside the two-byte character
        (d / "00000000.jsonl").write_bytes(blob[:cut])
        (d / "00000001.jsonl").write_bytes(blob[cut:])
        s = parse_antigravity(Path(td) / "sess")
        ok = (s is not None and [t.role for t in s.turns] == ["user", "assistant"] and s.turns[0].text.startswith("şifre testi")
              and s.turns[1].tools == [("run_command", "ls -la")] and s.turns[1].text == "done ✓" and s.cwd == "/tmp/proj"
              and "SECRET-OUTPUT" not in render(s)[0] and "hidden" not in render(s)[0])
        bad += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} antigravity chunk-boundary parse")
    # muse: plain and retained_frame-wrapped records, child streams ignored
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "01aaaaaa-0000-7000-8000-000000000001"
        d.mkdir()
        sid = d.name

        def rec(pt, payload, stream=sid):
            return {"stream": {"kind": "session", "id": stream}, "recorded_at": 1793216232492921, "payload_type": pt, "payload": payload}

        def run(ev, stream=sid):
            return rec("runtime.session", {"kind": "run", "event": ev}, stream)
        lines = [rec("runtime.session.metadata", {"record": {"workspace_root": "/tmp/w"}}),
                 run({"kind": "started", "prompt": "connect the brain"}),
                 run({"kind": "assistant_tool_calls_committed", "tool_calls": [{"name": "bash", "args": json.dumps({"command": "ls"})}]}),
                 run({"kind": "started", "prompt": "CHILD-ONLY"}, stream="other"),
                 {"retained_frame": "x", "children": [{"record_json": json.dumps(run({"kind": "assistant_message_committed", "text": "connected ✓"}))}]},
                 run({"kind": "reasoning_committed", "encrypted_content": "SECRET-REASONING"})]
        (d / "session.jsonl").write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
        s = parse_muse(d / "session.jsonl")
        out = render(s)[0]
        ok = ([t.role for t in s.turns] == ["user", "assistant"] and s.turns[1].text == "connected ✓"
              and s.turns[1].tools == [("bash", "ls")] and s.cwd == "/tmp/w" and "CHILD-ONLY" not in out and "SECRET-REASONING" not in out)
        bad += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} muse session parse")

    # claude-web keeps a short question with a real answer and a long message with a failed reply, and drops empty
    # shells; agent harnesses keep the noise filter (a short question there is not a conversation worth archiving)
    def chat(harness, user, asst):
        s = Session(harness, "t")
        s.turns += [Turn("user", None, user)] if user else []
        s.turns += [Turn("assistant", None, asst)] if asst else []
        return s
    ok = (usable(chat("claude-web", "French protesting", "x" * 1500)) and usable(chat("claude-web", "y" * 130, ""))
          and not usable(chat("claude-web", "", "")) and not usable(chat("claude-web", "hi", "hello"))
          and not usable(chat("claude-code", "French protesting", "x" * 1500)))
    bad += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} usable(): claude-web keeps short Q&A, agents keep the noise filter")
    # gemini: records group by chat URL, html becomes text, feedback events and images are dropped
    with tempfile.TemporaryDirectory() as td:
        url = "https://gemini.google.com/app/abc123"
        recs = [{"title": "Prompted second", "time": "2026-08-01T10:05:00Z", "details": [{"name": url}],
                 "safeHtmlItem": [{"html": "<p>two <strong>b</strong></p><img src=x><ul><li>i</li></ul>"}]},
                {"title": "Prompted first &amp; a", "time": "2026-08-01T10:00:00Z", "details": [{"name": url}],
                 "subtitles": [{"name": "Attached 1 file."}, {"name": "-  shot.png", "url": "shot.png"}],
                 "safeHtmlItem": [{"html": "<p>one</p>"}]},
                {"title": "Cleared previous feedback", "time": "2026-08-01T10:06:00Z"}]
        f = Path(td) / "MyActivity.json"
        f.write_text(json.dumps(recs), encoding="utf-8")
        ss = list(iter_gemini_takeout(f))
        ok = (len(ss) == 1 and [t.role for t in ss[0].turns] == ["user", "assistant", "user", "assistant"]
              and ss[0].turns[0].text == "first & a" and ss[0].turns[0].tools == [("attachment", "shot.png")]
              and ss[0].turns[3].text == "two **b**\n\n- i")
        bad += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} gemini takeout parse")
    print("selftest:", "PASS" if bad == 0 else f"{bad} FAILED")
    return bad == 0


# ----------------------------------------------------------------------------- model
class Turn:
    __slots__ = ("role", "ts", "text", "tools")

    def __init__(self, role, ts, text="", tools=None):
        self.role, self.ts, self.text, self.tools = role, ts, text, tools or []


class Session:
    def __init__(self, harness, sid):
        self.harness, self.sid = harness, sid
        self.title = None
        self.cwd = None
        self.branch = None
        self.model = None
        self.started = None
        self.ended = None
        self.turns = []
        self.source = None

    def touch(self, ts):
        if not ts:
            return
        if self.started is None or ts < self.started:
            self.started = ts
        if self.ended is None or ts > self.ended:
            self.ended = ts


def parse_ts(v):
    """ISO string or epoch (s/ms) -> aware datetime (UTC if no offset)."""
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int, float)):
            return dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=dt.timezone.utc)
        s = str(v).replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


# ----------------------------------------------------------------------------- shared cleaning
R_SYSREM = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
R_TAGBLOCKS = re.compile(
    r"<(command-name|command-message|command-args|local-command-stdout|local-command-stderr|local-command-caveat|"
    r"user-prompt-submit-hook|ide_opened_file|ide_selection|environment_context|user_instructions|INSTRUCTIONS)>.*?</\1>",
    re.S,
)
R_AGENTS_PREAMBLE = re.compile(r"^# AGENTS\.md instructions for .*?</INSTRUCTIONS>\s*", re.S)


def clean_user(text):
    text = R_SYSREM.sub("", text)
    text = R_TAGBLOCKS.sub("", text)
    text = R_AGENTS_PREAMBLE.sub("", text)
    return text.strip()


def one_line(s, n=140):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_summary(name, inp):
    inp = inp if isinstance(inp, dict) else {}
    pick = lambda *ks: next((inp[k] for k in ks if inp.get(k)), None)
    n = name or "tool"
    if n in ("Bash", "bash", "shell"):
        t = pick("command", "cmd")
    elif n in ("Read", "Write", "Edit", "NotebookEdit", "read", "write", "edit", "MultiEdit"):
        t = pick("file_path", "filePath", "path", "notebook_path")
    elif n in ("Glob", "Grep", "glob", "grep"):
        t = pick("pattern", "query")
    elif n in ("WebFetch", "webfetch"):
        t = pick("url")
    elif n in ("WebSearch", "websearch"):
        t = pick("query")
    elif n in ("Agent", "Task", "task"):
        t = pick("description", "subagent_type", "prompt")
    elif n == "Skill":
        t = pick("skill", "name")
    else:
        t = pick("description", "query", "url", "path", "file_path", "command", "name")
        if t is None and inp:
            t = "(" + ", ".join(sorted(inp)[:5]) + ")"
    return n, one_line(t or "")


# ----------------------------------------------------------------------------- adapters
def parse_claude_code(path):
    s = Session("claude-code", path.stem)
    s.source = str(path)
    cur = None
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            e = json.loads(line)
        except Exception:
            continue
        t = e.get("type")
        if t == "custom-title":
            s.title = e.get("customTitle") or s.title
            continue
        if t not in ("user", "assistant"):
            continue
        if e.get("isSidechain") or e.get("isMeta") or e.get("isCompactSummary"):
            continue
        ts = parse_ts(e.get("timestamp"))
        s.touch(ts)
        s.cwd = s.cwd or e.get("cwd")
        s.branch = s.branch or e.get("gitBranch")
        msg = e.get("message") or {}
        c = msg.get("content")
        if t == "user":
            texts = []
            if isinstance(c, str):
                texts = [c]
            elif isinstance(c, list):
                texts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
            text = clean_user("\n".join(texts))
            cur = None
            if text:
                s.turns.append(Turn("user", ts, text))
        else:
            s.model = msg.get("model") or s.model
            if not isinstance(c, list):
                continue
            for b in c:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and (b.get("text") or "").strip():
                    if cur is None:
                        cur = Turn("assistant", ts)
                        s.turns.append(cur)
                    cur.text += ("\n\n" if cur.text else "") + b["text"].strip()
                elif b.get("type") == "tool_use":
                    if cur is None:
                        cur = Turn("assistant", ts)
                        s.turns.append(cur)
                    cur.tools.append(tool_summary(b.get("name"), b.get("input")))
    return s


def _codex_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return content.get("text", "")
    out = []
    for b in content or []:
        if isinstance(b, dict) and isinstance(b.get("text"), str):
            out.append(b["text"])
    return "\n".join(out)


def parse_codex(path):
    s = None
    cur = None
    skip = False
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            e = json.loads(line)
        except Exception:
            continue
        pl = e.get("payload") if isinstance(e.get("payload"), dict) else {}
        et = e.get("type")
        if et == "session_meta":
            sid = pl.get("id") or pl.get("session_id") or path.stem
            s = Session("codex", sid)
            s.source = str(path)
            s.cwd = pl.get("cwd")
            s.model = pl.get("model_provider")
            s.touch(parse_ts(pl.get("timestamp")))
            if pl.get("parent_thread_id") or pl.get("forked_from_id"):
                skip = True      # sub-agent / forked thread: the parent session carries the summary
            continue
        if s is None or skip:
            continue
        if et == "event_msg" and pl.get("type") == "item_completed":
            it = pl.get("item") or {}
            ty = it.get("type")
            ts = parse_ts(e.get("timestamp"))
            s.touch(ts)
            if ty == "UserMessage":
                text = clean_user(_codex_text(it.get("content")))
                cur = None
                if text:
                    s.turns.append(Turn("user", ts, text))
            elif ty == "AgentMessage":
                text = _codex_text(it.get("content")).strip()
                if text:
                    if cur is None:
                        cur = Turn("assistant", ts)
                        s.turns.append(cur)
                    cur.text += ("\n\n" if cur.text else "") + text
            elif ty in ("CommandExecution", "FileChange", "McpToolCall", "WebSearch"):
                if cur is None:
                    cur = Turn("assistant", ts)
                    s.turns.append(cur)
                if ty == "CommandExecution":
                    cmd = it.get("command")
                    cmd = " ".join(cmd) if isinstance(cmd, list) else cmd
                    cur.tools.append(("Bash", one_line(cmd)))
                elif ty == "FileChange":
                    cur.tools.append(("Edit", one_line(", ".join(sorted((it.get("changes") or {}).keys())[:4]))))
                elif ty == "McpToolCall":
                    cur.tools.append((f"mcp:{it.get('server')}", one_line(it.get("tool"))))
                else:
                    cur.tools.append(("WebSearch", one_line(it.get("query"))))
    return None if skip else s


def iter_opencode(db):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.execute("PRAGMA query_only=1")
    for sid, title, directory, model, tc, tu in c.execute(
        "select id, title, directory, model, time_created, time_updated from session "
        "where parent_id is null order by time_created"
    ):
        s = Session("opencode", sid)
        s.title, s.cwd, s.source = title, directory, f"{db}#{sid}"
        s.model = None
        if isinstance(model, str) and model:
            try:
                mj = json.loads(model) if model.lstrip().startswith("{") else None
            except Exception:
                mj = None
            s.model = (f"{mj.get('providerID', '')}/{mj.get('id', '')}".strip("/") if isinstance(mj, dict) else model)
        s.touch(parse_ts(tc))
        s.touch(parse_ts(tu))
        roles = {}
        for mid, mt, data in c.execute(
            "select id, time_created, data from message where session_id=? order by time_created, id", (sid,)
        ):
            try:
                roles[mid] = (json.loads(data).get("role"), mt)
            except Exception:
                roles[mid] = (None, mt)
        by_msg = {}
        for mid, pt, data in c.execute(
            "select message_id, time_created, data from part where session_id=? order by time_created, id", (sid,)
        ):
            by_msg.setdefault(mid, []).append((pt, data))
        for mid, (role, mt) in sorted(roles.items(), key=lambda kv: kv[1][1]):
            ts = parse_ts(mt)
            texts, tools = [], []
            for pt, data in by_msg.get(mid, []):
                try:
                    j = json.loads(data)
                except Exception:
                    continue
                if j.get("type") == "text" and not j.get("synthetic") and (j.get("text") or "").strip():
                    texts.append(j["text"].strip())
                elif j.get("type") == "tool":
                    st = j.get("state") if isinstance(j.get("state"), dict) else {}
                    tools.append(tool_summary(j.get("tool"), st.get("input")))
            if role == "user":
                text = clean_user("\n".join(texts))
                if text:
                    s.turns.append(Turn("user", ts, text))
            elif role == "assistant" and (texts or tools):
                s.turns.append(Turn("assistant", ts, "\n\n".join(texts), tools))
        yield s


R_AGY_REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
AGY_CHUNKS = Path(".system_generated") / "logs" / "chunks" / "transcript"


def _agy_val(v):
    """Antigravity stores tool args JSON-quoted ('"ls -la"'); unwrap one level."""
    if isinstance(v, str):
        try:
            j = json.loads(v)
        except Exception:
            return v
        return j if isinstance(j, str) else v
    return v


def agy_tool(name, args):
    a = {k: _agy_val(v) for k, v in args.items()} if isinstance(args, dict) else {}
    inp = {}
    for dst, srcs in (("command", ("CommandLine",)), ("file_path", ("AbsolutePath", "TargetFile", "File", "DirectoryPath")),
                      ("query", ("Query", "SearchPath")), ("url", ("Url", "URL"))):
        v = next((a[k] for k in srcs if a.get(k)), None)
        if v:
            inp[dst] = v
    if not inp:
        inp["description"] = a.get("toolSummary") or a.get("toolAction") or ""
    return tool_summary(name, inp)


def agy_chunk_dir(sdir):
    return Path(sdir) / AGY_CHUNKS


def parse_antigravity(sdir):
    """~/.gemini/antigravity/brain/<session>/.system_generated/logs/chunks/transcript/*.jsonl

    The chunks are fixed-size slices of one stream and cut lines mid-record, so they are joined as bytes first.
    """
    chunks = sorted(agy_chunk_dir(sdir).glob("*.jsonl"))
    if not chunks:
        return None
    s = Session("antigravity", Path(sdir).name)
    s.source = str(sdir)
    for line in b"".join(p.read_bytes() for p in chunks).decode("utf-8", "replace").splitlines():
        try:
            e = json.loads(line)
        except Exception:
            continue  # a record truncated by the exporter itself
        ts = parse_ts(e.get("created_at"))
        s.touch(ts)
        typ = e.get("type")
        if typ == "USER_INPUT" and e.get("source") == "USER_EXPLICIT":
            c = e.get("content") or ""
            m = R_AGY_REQUEST.search(c)
            text = clean_user(m.group(1) if m else c)
            if text:
                s.turns.append(Turn("user", ts, text))
        elif typ == "PLANNER_RESPONSE":
            text = (e.get("content") or "").strip()
            calls = e.get("tool_calls") if isinstance(e.get("tool_calls"), list) else []
            tools = [agy_tool(c.get("name"), c.get("args")) for c in calls if isinstance(c, dict)]
            for c in calls:
                cwd = _agy_val((c.get("args") or {}).get("Cwd")) if isinstance(c, dict) else None
                if cwd and not s.cwd:
                    s.cwd = cwd
            if not (text or tools):
                continue
            if s.turns and s.turns[-1].role == "assistant":
                last = s.turns[-1]
                last.text = (last.text + "\n\n" + text).strip() if text else last.text
                last.tools += tools
            else:
                s.turns.append(Turn("assistant", ts, text, tools))
    return s


def _muse_records(path):
    """session.jsonl: plain records, or 'retained_frame' wrappers whose children carry the record as a JSON string."""
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            e = json.loads(line)
        except Exception:
            continue
        if "retained_frame" in e:
            for ch in e.get("children") or []:
                try:
                    yield json.loads(ch["record_json"])
                except Exception:
                    continue
        else:
            yield e


def parse_muse(path):
    """~/.local/share/muse/sessions/<yyyy>/<mm>/<dd>/<session-id>/session.jsonl (Meta's Muse Code)."""
    path = Path(path)
    s = Session("muse", path.parent.name)
    s.source = str(path)
    for r in _muse_records(path):
        pt, p = r.get("payload_type"), r.get("payload") or {}
        if (r.get("stream") or {}).get("id") != s.sid:
            continue  # reminder / child streams
        ts = parse_ts((r.get("recorded_at") or 0) / 1e6) if r.get("recorded_at") else None
        s.touch(ts)
        if pt == "runtime.session.metadata":
            rec = p.get("record") or {}
            s.cwd = s.cwd or rec.get("workspace_root")
            s.model = s.model or rec.get("model_id")
        elif pt == "runtime.session" and p.get("kind") == "run":
            e = p.get("event") or {}
            k = e.get("kind")
            if k == "started" and (e.get("prompt") or "").strip():
                s.turns.append(Turn("user", ts, clean_user(e["prompt"])))
            elif k in ("assistant_message_committed", "assistant_tool_calls_committed"):
                text = (e.get("text") or "").strip() if k == "assistant_message_committed" else ""
                tools = []
                for c in e.get("tool_calls") or []:
                    try:
                        args = json.loads(c.get("args") or "{}")
                    except Exception:
                        args = {}
                    tools.append(tool_summary(c.get("name") or c.get("tool_name"), args))
                if s.turns and s.turns[-1].role == "assistant":
                    last = s.turns[-1]
                    last.text = (last.text + "\n\n" + text).strip() if text else last.text
                    last.tools += tools
                elif text or tools:
                    s.turns.append(Turn("assistant", ts, text, tools))
    return s


def iter_claude_web(path, skip_ids=frozenset(), skip_until=None):
    """claude.ai export (conversations.json). Attachment contents, thinking and tool results are dropped."""
    for c in json.load(open(path, encoding="utf-8")):
        if c["uuid"] in skip_ids and (skip_until is None or (parse_ts(c.get("updated_at")) or skip_until) <= skip_until):
            continue
        s = Session("claude-web", c["uuid"])
        s.source = str(path)
        s.title = c.get("name") or ""
        for m in c.get("chat_messages") or []:
            ts = parse_ts(m.get("created_at"))
            s.touch(ts)
            items = m.get("content") or []
            texts = [i["text"].strip() for i in items if i.get("type") == "text" and (i.get("text") or "").strip()]
            if not texts and (m.get("text") or "").strip():
                texts = [m["text"].strip()]
            tools = [tool_summary(i.get("name"), i.get("input")) for i in items if i.get("type") == "tool_use"]
            tools += [("attachment", one_line(a.get("file_name"))) for a in m.get("attachments") or []]
            role = "user" if m.get("sender") == "human" else "assistant"
            text = "\n\n".join(texts)
            if role == "user":
                text = clean_user(text)
            if text or tools:
                s.turns.append(Turn(role, ts, text, tools))
        yield s


def legacy_claude_ids(root):
    """session_ids of claude.ai chats already imported into the archive's legacy/ tree, and its newest date."""
    ids, newest = set(), None
    for f in Path(root).glob("*.md"):
        for line in open(f, encoding="utf-8", errors="replace"):
            if line.startswith("date:"):
                d = parse_ts(line.split(":", 1)[1].strip().strip("'\""))
                newest = d if d and (newest is None or d > newest) else newest
            elif line.strip().startswith("session_id:"):
                ids.add(line.split(":", 1)[1].strip())
                break
    return ids, newest


class _HtmlText(HTMLParser):
    """safeHtmlItem -> markdown-ish text; images and unknown tags vanish, their text stays."""
    MARKS = {"strong": "**", "b": "**", "em": "*", "i": "*"}
    BREAKS = {"p", "div", "ul", "ol", "table", "tr", "pre", "hr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.pre = [], False

    def handle_starttag(self, tag, attrs):
        if tag in self.BREAKS:
            self.out.append("\n\n")
        if tag == "pre":
            self.out.append("```\n")
            self.pre = True
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "br":
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append("| ")
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("#" * int(tag[1]) + " ")
        elif tag in self.MARKS:
            self.out.append(self.MARKS[tag])

    def handle_endtag(self, tag):
        if tag == "pre":
            self.out.append("\n```\n\n")
            self.pre = False
        elif tag in ("td", "th"):
            self.out.append(" ")
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag in self.MARKS:
            self.out.append(self.MARKS[tag])
        elif tag in self.BREAKS:
            self.out.append("\n\n")

    def handle_data(self, data):
        self.out.append(data)


def html_to_text(h):
    p = _HtmlText()
    p.feed(h or "")
    p.close()
    return re.sub(r"\n{3,}", "\n\n", "".join(p.out)).strip()


def iter_gemini_takeout(path):
    """Google Takeout 'My Activity > Gemini Apps' MyActivity.json: one record per prompt, grouped into chats by the
    /app/<id> URL. Attachments stay as names only. Feedback/draft events are dropped; a Canvas with a body is its own session."""
    chats = {}
    for r in sorted(json.load(open(path, encoding="utf-8")), key=lambda x: x.get("time") or ""):
        title, ts, subs = r.get("title") or "", parse_ts(r.get("time")), r.get("subtitles") or []
        cid = next((d["name"].rsplit("/", 1)[1] for d in r.get("details") or [] if "/app/" in d.get("name", "")), None)
        if title.startswith("Created Gemini Canvas") and not cid:
            body = max((x.get("name") or "" for x in subs), key=len, default="")
            if len(body) < MIN_ASSIST_CHARS:
                continue
            cid = "canvas" + re.sub(r"\D", "", r.get("time") or "")
            s = chats[cid] = Session("gemini", cid)
            s.title = title
            s.touch(ts)
            s.turns += [Turn("user", ts, title), Turn("assistant", ts, body)]
            continue
        if not cid or not title.startswith("Prompted"):
            continue
        s = chats.get(cid)
        if s is None:
            s = chats[cid] = Session("gemini", cid)
            s.source = str(path)
        s.touch(ts)
        text = clean_user(html.unescape(title[len("Prompted"):]).strip())
        files = [("attachment", one_line((x.get("name") or "")[1:])) for x in subs if (x.get("name") or "").startswith("-")]
        images = [("generated image", "")] if any("generated image" in (x.get("name") or "") for x in subs) else []
        reply = "\n\n".join(t for t in (html_to_text(i.get("html")) for i in r.get("safeHtmlItem") or []) if t)
        if text or files:
            s.turns.append(Turn("user", ts, text, files))
        if reply or images:
            s.turns.append(Turn("assistant", ts, reply, images))
    return list(chats.values())


# ----------------------------------------------------------------------------- output
def render(s):
    n_red = 0

    def R(x):
        nonlocal n_red
        y, k = redact(x or "")
        n_red += k
        return y

    first_user = next((t.text for t in s.turns if t.role == "user"), "")
    title = R(one_line(s.title or first_user, 90)) or "(untitled)"
    body = []
    tool_calls = 0
    for t in s.turns:
        hhmm = t.ts.astimezone(dt.timezone.utc).strftime("%H:%M") if t.ts else ""
        body.append(f"**{t.role}** · {hhmm}Z\n")
        if t.text:
            body.append(R(t.text) + "\n")
        for name, target in t.tools:
            tool_calls += 1
            body.append(f"- `{name}` — {R(target)}" if target else f"- `{name}`")
        if t.tools:
            body.append("")
    fm = {
        "type": "conversation",
        "harness": s.harness,
        "host": HOST,
        "session_id": s.sid,
        "title": title,
        "project": R(s.cwd or ""),
        "branch": s.branch or "",
        "model": s.model or "",
        "started": s.started.isoformat() if s.started else "",
        "ended": s.ended.isoformat() if s.ended else "",
        "turns": len(s.turns),
        "tool_calls": tool_calls,
        "redactions": n_red,
        "extractor": VERSION,
    }
    head = "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items()) + "\n---\n"
    return head + f"\n# {title}\n\n" + "\n".join(body).rstrip() + "\n", n_red


def out_path(out_root, s):
    st = (s.started or dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)).astimezone(dt.timezone.utc)
    sid8 = re.sub(r"[^A-Za-z0-9]", "", s.sid)[-8:] or "unknown"
    return Path(out_root) / "conversations" / s.harness / st.strftime("%Y") / st.strftime("%m") / (
        st.strftime("%Y%m%d-%H%M") + "-" + sid8 + ".md"
    )


def usable(s):
    u = sum(len(t.text) for t in s.turns if t.role == "user")
    a = sum(len(t.text) for t in s.turns if t.role == "assistant")
    if s.harness in ("claude-web", "gemini"):
        # a person chatting: a one-line question with a real answer is the point, and a long message is kept even
        # when the reply failed. Only empty shells are noise.
        return (u >= 1 and a >= MIN_ASSIST_CHARS) or u >= MIN_USER_CHARS
    return u >= MIN_USER_CHARS and a >= MIN_ASSIST_CHARS


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out")
    ap.add_argument("--harness", default="all", choices=["all", "claude-code", "codex", "opencode", "antigravity", "muse", "claude-web", "gemini"])
    ap.add_argument("--antigravity-root", default=os.path.expanduser("~/.gemini/antigravity/brain"))
    ap.add_argument("--muse-root", default=os.path.expanduser("~/.local/share/muse/sessions"))
    ap.add_argument("--claude-web-export", help="claude.ai export conversations.json (manual, one-off)")
    ap.add_argument("--gemini-takeout", help="Google Takeout My Activity/Gemini Apps/MyActivity.json (manual, one-off)")
    ap.add_argument("--claude-web-legacy", help="archive legacy/conversations/claude dir; skip chats already there")
    ap.add_argument("--claude-root", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--codex-root", default=os.path.expanduser("~/.codex/sessions"))
    ap.add_argument("--opencode-db", default=os.path.expanduser("~/.local/share/opencode/opencode.db"))
    ap.add_argument("--since-days", type=float, default=None, help="only files/sessions updated within N days")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if selftest() else 1)
    if not a.out:
        ap.error("--out is required")
    cutoff = None
    if a.since_days is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=a.since_days)

    def recent(path):
        return cutoff is None or dt.datetime.fromtimestamp(os.path.getmtime(path), tz=dt.timezone.utc) >= cutoff

    sources = []
    if a.harness in ("all", "claude-code") and os.path.isdir(a.claude_root):
        for proj in sorted(Path(a.claude_root).iterdir()):
            if proj.is_dir():
                sources += [("claude-code", f) for f in sorted(proj.glob("*.jsonl")) if recent(f)]
    if a.harness in ("all", "codex") and os.path.isdir(a.codex_root):
        sources += [("codex", f) for f in sorted(Path(a.codex_root).rglob("*.jsonl")) if recent(f)]
    if a.harness in ("all", "antigravity") and os.path.isdir(a.antigravity_root):
        for sd in sorted(Path(a.antigravity_root).iterdir()):
            cs = sorted(agy_chunk_dir(sd).glob("*.jsonl")) if sd.is_dir() else []
            if cs and recent(cs[-1]):
                sources.append(("antigravity", sd))
    if a.harness in ("all", "muse") and os.path.isdir(a.muse_root):
        sources += [("muse", f) for f in sorted(Path(a.muse_root).glob("*/*/*/*/session.jsonl")) if recent(f)]
    stats = {}

    def bump(h, k, v=1):
        stats.setdefault(h, {}).setdefault(k, 0)
        stats[h][k] += v

    def emit(s):
        bump(s.harness, "seen")
        if not usable(s):
            bump(s.harness, "dropped_small")
            return
        text, nred = render(s)
        p = out_path(a.out, s)
        bump(s.harness, "redactions", nred)
        if p.exists() and p.read_text(encoding="utf-8") == text:
            bump(s.harness, "unchanged")
            return
        bump(s.harness, "written")
        bump(s.harness, "bytes", len(text.encode("utf-8")))
        if not a.dry_run:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")

    n = 0
    for h, f in sources:
        if a.limit and n >= a.limit:
            break
        n += 1
        try:
            s = {"claude-code": parse_claude_code, "antigravity": parse_antigravity, "muse": parse_muse}.get(h, parse_codex)(f)
        except Exception as ex:
            bump(h, "errors")
            print(f"  ! {h} {f.name}: {ex}", file=sys.stderr)
            continue
        if s is None:
            bump(h, "dropped_subagent")
            continue
        emit(s)
    if a.harness in ("all", "opencode") and os.path.exists(a.opencode_db):
        for s in iter_opencode(a.opencode_db):
            if cutoff and s.ended and s.ended < cutoff:
                continue
            emit(s)
    if a.harness in ("all", "claude-web") and a.claude_web_export:
        ids, newest = legacy_claude_ids(a.claude_web_legacy) if a.claude_web_legacy else (set(), None)
        until = newest + dt.timedelta(days=1) if newest else None
        for s in iter_claude_web(a.claude_web_export, ids, until):
            emit(s)
    if a.harness in ("all", "gemini") and a.gemini_takeout:
        for s in iter_gemini_takeout(a.gemini_takeout):
            emit(s)
    print(f"{VERSION} on {HOST} -> {a.out}{' (dry run)' if a.dry_run else ''}")
    for h, d in sorted(stats.items()):
        print(f"  {h:12s} " + "  ".join(f"{k}={v}" for k, v in sorted(d.items())))


if __name__ == "__main__":
    main()
