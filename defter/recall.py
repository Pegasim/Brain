"""Answer natural-language questions from Defter: curated notes first, conversations after.

Notes are ranked by reciprocal-rank fusion of FTS5 keyword search and bge-m3 passage similarity.
Passage vectors are a cache keyed by passage hash and model. `embed` fills it; refresh never calls a model.
Without vectors or a reachable embedding server, recall uses keyword search alone and says so.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import urllib.request
from collections.abc import Callable
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import NotRequired, TypedDict

import numpy as np
from cfv.db import fold_tr

NOTE_KIND = "brain_note"
OTHER_KINDS = ("brain_conversation", "brain_session", "calendar")
PASSAGE_WORDS, PASSAGE_STEP = 220, 110
RRF_K = 60
CANDIDATES = 30
EXCERPT_LIMIT = 600
PREFIX = 5  # Turkish suffixes: match a word's first five letters (fixed-prefix stemming)

SCHEMA = """
CREATE TABLE IF NOT EXISTS passage (
    event_id INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL, hash TEXT NOT NULL, PRIMARY KEY (event_id, seq));
CREATE INDEX IF NOT EXISTS passage_hash ON passage (hash);
CREATE TABLE IF NOT EXISTS embedding (
    hash TEXT NOT NULL, model TEXT NOT NULL, vector BLOB NOT NULL, PRIMARY KEY (hash, model));
"""

Embedder = Callable[[list[str]], list[list[float]]]
_TOKEN = re.compile(r"\w+", re.UNICODE)
_PLAIN = str.maketrans("ıİçğöşüâîûÇĞÖŞÜÂÎÛ", "iicgosuaiucgosuaiu")  # one character each, so offsets survive
_VECTORS: dict[tuple, tuple] = {}


class RecallResult(TypedDict):
    evidence: list[dict]
    semantic: bool
    warning: NotRequired[str]


def settings() -> tuple[str, str]:
    """Embedding endpoint (OpenAI-compatible /v1/embeddings) and model, from the environment."""
    return (os.environ.get("DEFTER_EMBED_URL", "http://localhost:11434/v1/embeddings"),
            os.environ.get("DEFTER_EMBED_MODEL", "bge-m3"))


def http_embedder(url: str, model: str, timeout: float = 120) -> Embedder:
    def embed(texts):
        request = urllib.request.Request(url, data=json.dumps({"model": model, "input": texts}).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read())["data"]
        return [item["embedding"] for item in sorted(data, key=lambda item: item["index"])]
    return embed


def passages(body: str) -> list[str]:
    """Overlapping windows of 220 words, every word covered, no window contained in the previous one."""
    words = body.split()
    return [" ".join(words[i:i + PASSAGE_WORDS])
            for i in range(0, max(len(words) - PASSAGE_STEP, 1), PASSAGE_STEP)]


def passage_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def index_passages(conn: sqlite3.Connection):
    """Rebuild the note passage map. Called by refresh inside its staging transaction."""
    conn.execute("DELETE FROM passage")
    rows = conn.execute("SELECT id, body FROM event WHERE kind=?", (NOTE_KIND,)).fetchall()
    conn.executemany("INSERT INTO passage VALUES (?, ?, ?)", [
        (event_id, seq, passage_hash(text)) for event_id, body in rows for seq, text in enumerate(passages(body or ""))])


def _unit(vectors) -> np.ndarray:
    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms == 0, 1, norms)


def embed(path: Path, embedder: Embedder | None = None, model: str | None = None, batch: int = 16,
          progress: Callable[[int, int], None] | None = None) -> dict:
    """Embed note passages that lack a vector for the model, committing per batch; drop vectors no passage uses."""
    url, default_model = settings()
    model = model or default_model
    embedder = embedder or http_embedder(url, model)
    with open(path.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with closing(sqlite3.connect(path)) as conn:
            if not conn.execute("SELECT name FROM sqlite_master WHERE name='passage'").fetchone():
                raise ValueError("No passage table. Run refresh first.")
            rows = conn.execute(
                "SELECT p.hash, p.seq, e.body FROM passage p JOIN event e ON e.id=p.event_id "
                "WHERE NOT EXISTS (SELECT 1 FROM embedding m WHERE m.hash=p.hash AND m.model=?)", (model,))
            todo = {}
            for digest, seq, body in rows:
                todo.setdefault(digest, passages(body or "")[seq])
            items = list(todo.items())
            for start in range(0, len(items), batch):
                chunk = items[start:start + batch]
                vectors = _unit(embedder([text for _, text in chunk]))
                with conn:
                    conn.executemany("INSERT OR REPLACE INTO embedding VALUES (?, ?, ?)",
                                     [(digest, model, vector.tobytes()) for (digest, _), vector in zip(chunk, vectors)])
                if progress:
                    progress(min(start + batch, len(items)), len(items))
            with conn:
                pruned = conn.execute("DELETE FROM embedding WHERE hash NOT IN (SELECT hash FROM passage)").rowcount
            total = conn.execute("SELECT count(*) FROM passage").fetchone()[0]
    return {"model": model, "embedded": len(items), "pruned": pruned, "passages": total}


def keyword_query(text: str, every: bool = False) -> str:
    """The question's words, Turkish-folded; words longer than five letters match by their first five.

    Any word may match, or with every=True each word of four or more letters must appear. Whole conversations
    are long enough to contain most common words, so they need the stricter form.
    """
    tokens = dict.fromkeys(t for t in _TOKEN.findall(fold_tr(text).lower()) if len(t) > (3 if every else 1))
    return (" AND " if every else " OR ").join(f'"{t[:PREFIX]}"*' if len(t) > PREFIX else f'"{t}"' for t in tokens)


def excerpt(text: str, query: str) -> str:
    """A window of the text around its first occurrence of the question's most specific word."""
    plain = text.translate(_PLAIN).lower()
    words = sorted({t[:PREFIX + 3] for t in _TOKEN.findall(query.translate(_PLAIN).lower()) if len(t) > 3}, key=len,
                   reverse=True)
    position = next((p for p in (plain.find(w) for w in words) if p >= 0), 0)
    begin = max(0, min(position - EXCERPT_LIMIT // 3, len(text) - EXCERPT_LIMIT))
    return ("…" if begin else "") + text[begin:begin + EXCERPT_LIMIT]


def _dates(start, end) -> tuple[list[str], list[str]]:
    where, args = [], []
    if start:
        where.append("e.local_date >= ?")
        args.append(start)
    if end:
        where.append("e.local_date <= ?")
        args.append(end)
    return where, args


def _keyword(conn, query: str, kinds: tuple[str, ...], start, end, limit: int, every: bool = False) -> list[int]:
    match = keyword_query(query, every)
    if not match:
        return []
    where, args = _dates(start, end)
    rows = conn.execute(
        "SELECT e.id FROM event_fts JOIN event e ON e.id=event_fts.rowid "
        f"WHERE event_fts MATCH ? AND e.kind IN ({','.join('?' * len(kinds))}) {''.join(' AND ' + w for w in where)} "
        "ORDER BY bm25(event_fts) LIMIT ?", (match, *kinds, *args, limit))
    return [row[0] for row in rows]


def _vectors(conn, path: Path, model: str):
    """Note passage vectors for the model, cached per database file version."""
    stat = path.stat()
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size, model)
    if key not in _VECTORS:
        rows = conn.execute(
            "SELECT p.event_id, p.seq, e.local_date, m.vector FROM passage p "
            "JOIN embedding m ON m.hash=p.hash AND m.model=? JOIN event e ON e.id=p.event_id "
            "WHERE e.kind=? ORDER BY p.event_id, p.seq", (model, NOTE_KIND)).fetchall()
        _VECTORS.clear()
        if rows:
            _VECTORS[key] = (np.array([r[0] for r in rows]), np.array([r[1] for r in rows]),
                             np.array([r[2] for r in rows]), np.frombuffer(b"".join(r[3] for r in rows),
                                                                           dtype=np.float32).reshape(len(rows), -1))
        else:
            _VECTORS[key] = None
    return _VECTORS[key]


def _semantic(conn, path, query, start, end, embedder) -> tuple[list[int], dict[int, int], str | None]:
    """Notes ranked by their best passage's cosine similarity, the best passage per note, and any reason it failed."""
    url, model = settings()
    if not conn.execute("SELECT name FROM sqlite_master WHERE name='embedding'").fetchone():
        return [], {}, "no passage vectors: run refresh, then embed"
    index = _vectors(conn, path, model)
    if index is None:
        return [], {}, f"no passage vectors for {model}: run embed"
    owners, seqs, days, matrix = index
    try:
        vector = _unit((embedder or http_embedder(url, model, timeout=30))([query]))[0]
    except Exception as exc:  # an unreachable or failing server must not break keyword recall
        return [], {}, f"embedding server failed ({type(exc).__name__}): keyword results only"
    if vector.shape[0] != matrix.shape[1]:
        return [], {}, f"query vector has {vector.shape[0]} dimensions, stored vectors {matrix.shape[1]}: rerun embed"
    sims = matrix @ vector
    keep = np.ones(len(sims), dtype=bool)
    if start:
        keep &= days >= start
    if end:
        keep &= days <= end
    best, passage = {}, {}
    for i in np.argsort(-sims):
        if not keep[i]:
            continue
        owner = int(owners[i])
        if owner not in best:
            best[owner], passage[owner] = float(sims[i]), int(seqs[i])
            if len(best) >= CANDIDATES:
                break
    return list(best), passage, None


def recall(path: Path, query: str, start: str | None = None, end: str | None = None, limit: int = 8,
           conversations: int = 4, embedder: Embedder | None = None) -> RecallResult:
    """Notes ranked by keyword and passage similarity together, then the best-matching conversations."""
    if not query.strip() or len(query) > 500:
        raise ValueError("Use a question of 1 to 500 characters")
    for value in (start, end):
        if value:
            date.fromisoformat(value)
    if start and end and start > end:
        raise ValueError("The start date must be on or before the end date")
    limit, conversations = max(1, min(limit, 30)), max(0, min(conversations, 20))
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        keyword = _keyword(conn, query, (NOTE_KIND,), start, end, CANDIDATES)
        semantic, best_passage, warning = _semantic(conn, path, query, start, end, embedder)
        scores: dict[int, float] = {}
        for ranking in (keyword, semantic):
            for rank, event_id in enumerate(ranking):
                scores[event_id] = scores.get(event_id, 0.0) + 1 / (RRF_K + rank + 1)
        notes = sorted(scores, key=lambda event_id: (-scores[event_id], event_id))[:limit]
        others = _keyword(conn, query, OTHER_KINDS, start, end, conversations, every=True) if conversations else []
        conn.row_factory = sqlite3.Row
        evidence = []
        for event_id in notes + others:
            row = conn.execute(
                "SELECT e.kind, e.start_utc, e.title, e.body, e.payload, s.raw_path, r.ref FROM event e "
                "JOIN source s ON s.id=e.source_id LEFT JOIN brain_reference r ON r.event_id=e.id WHERE e.id=?",
                (event_id,)).fetchone()
            payload = json.loads(row["payload"] or "{}")
            body = row["body"] or ""
            text = passages(body)[best_passage[event_id]] if event_id in best_passage else body
            evidence.append({
                "reference": row["ref"], "kind": row["kind"], "timestamp": row["start_utc"], "title": row["title"],
                "path": payload.get("note_path"), "source": payload.get("source_file") or row["raw_path"],
                "excerpt": excerpt(text, query), "supports": payload.get("evidence_refs", []),
                "matched": [name for name, ranking in (("keyword", keyword), ("semantic", semantic))
                            if event_id in ranking] if row["kind"] == NOTE_KIND else ["keyword"],
            })
    result = {"evidence": evidence, "semantic": warning is None}
    if warning:
        result["warning"] = warning
    return result
