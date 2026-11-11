"""Apple Notes gövdesi (ZICNOTEDATA.ZDATA: gzip'li protobuf) → biçimli satırlar.

Yapı (apple_cloud_notes_parser'dan bilinen şema):
  NoteStoreProto.2 = Document; Document.3 = Note
  Note.2 = metin (NSString → uzunluklar UTF-16 birimi), Note.5 = AttributeRun[]
  AttributeRun: 1 length, 2 ParagraphStyle{1 style_type, 4 indent, 5 Checklist{2 done}},
                5 font_weight (1 kalın, 2 italik, 3 ikisi), 7 strikethrough, 12 ek (attachment)
"""

from __future__ import annotations

import gzip
import zlib
from dataclasses import dataclass, field

from cfv.sources import protobuf as pb

STYLE = {0: "title", 1: "heading", 2: "subheading", 3: "mono", 100: "bullet", 101: "dash",
         102: "numbered", 103: "checkbox"}


@dataclass
class Line:
    text: str
    style: str = "body"
    indent: int = 0
    bold: bool = False          # satırdaki görünür metnin tamamı kalın mı
    checked: bool | None = None  # checkbox ise tik durumu
    strike: bool = False
    attachments: int = 0
    spans: list[tuple[str, bool]] = field(default_factory=list)  # (parça, kalın)


def _gunzip(data: bytes) -> bytes:
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    try:
        return zlib.decompress(data)
    except zlib.error:
        return data


def decode(data: bytes) -> list[Line]:
    root = pb.parse(_gunzip(data))
    note = pb.parse(pb.first(pb.parse(pb.first(root, 2)), 3))
    text: str = pb.first(note, 2, b"").decode("utf-8")
    u16 = text.encode("utf-16-le")

    # Her UTF-16 birimi için run özelliklerini çıkar, sonra satırlara böl.
    runs = []
    for raw in note.get(5, []):
        r = pb.parse(raw)
        ps = pb.parse(pb.first(r, 2, b""))
        chk = pb.parse(pb.first(ps, 5, b"")) if 5 in ps else None
        runs.append({
            "len": pb.first(r, 1, 0),
            "style": STYLE.get(pb.first(ps, 1, -1), "body"),
            "indent": pb.first(ps, 4, 0),
            "checked": (bool(pb.first(chk, 2, 0)) if chk is not None else None),
            "bold": pb.first(r, 5, 0) in (1, 3),
            "strike": bool(pb.first(r, 7, 0)),
            "attach": 12 in r,
        })

    lines: list[Line] = []
    cur = Line("")
    pos = 0  # UTF-16 birim konumu

    def flush():
        nonlocal cur
        cur.text = "".join(s for s, _ in cur.spans).replace("￼", "").rstrip()
        visible = [(s, b) for s, b in cur.spans if s.strip().replace("￼", "")]
        cur.bold = bool(visible) and all(b for _, b in visible)
        lines.append(cur)
        cur = Line("")

    for run in runs:
        chunk = u16[pos * 2 : (pos + run["len"]) * 2].decode("utf-16-le", errors="replace")
        pos += run["len"]
        for k, part in enumerate(chunk.split("\n")):
            if k > 0:
                flush()
            if part:
                if not cur.spans:  # paragraf stili satırın ilk run'ından
                    cur.style, cur.indent = run["style"], run["indent"]
                    cur.checked = run["checked"] if run["style"] == "checkbox" else None
                cur.spans.append((part, run["bold"]))
                cur.strike = cur.strike or run["strike"]
                cur.attachments += part.count("￼")
    if cur.spans:
        flush()
    return lines


def to_markdown(lines: list[Line]) -> str:
    out = []
    for ln in lines:
        pad = "  " * ln.indent
        if ln.style == "title":
            out.append(f"# {ln.text}")
        elif ln.style in ("heading", "subheading"):
            out.append(f"{'##' if ln.style == 'heading' else '###'} {ln.text}")
        elif ln.style == "checkbox":
            out.append(f"{pad}- [{'x' if ln.checked else ' '}] {ln.text}")
        elif ln.style in ("bullet", "dash", "numbered"):
            out.append(f"{pad}- {ln.text}")
        elif ln.bold and ln.text:
            out.append(f"**{ln.text}**")
        else:
            out.append(ln.text)
    return "\n".join(out)
