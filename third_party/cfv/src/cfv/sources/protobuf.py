"""Bağımlılıksız, minimal protobuf okuyucu (şemasız: alan numarası → değer listesi)."""

from __future__ import annotations


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def parse(buf: bytes) -> dict[int, list]:
    """Mesajı {alan: [değerler]} olarak döndür. Length-delimited alanlar bytes kalır;
    alt mesaj olarak yorumlamak çağıranın işidir (`parse(v)`)."""
    out: dict[int, list] = {}
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _varint(buf, i)
        elif wire == 1:
            val, i = int.from_bytes(buf[i : i + 8], "little"), i + 8
        elif wire == 2:
            ln, i = _varint(buf, i)
            val, i = buf[i : i + ln], i + ln
        elif wire == 5:
            val, i = int.from_bytes(buf[i : i + 4], "little"), i + 4
        else:
            raise ValueError(f"desteklenmeyen wire type {wire} (alan {field})")
        out.setdefault(field, []).append(val)
    return out


def first(msg: dict[int, list], field: int, default=None):
    vals = msg.get(field)
    return vals[0] if vals else default
