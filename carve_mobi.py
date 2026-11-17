#!/usr/bin/env python3
"""Carve PalmDB/MOBI containers from a raw image using BOOKMOBI signatures."""

from __future__ import annotations

import argparse
import re
import struct
from pathlib import Path


SIGNATURE = b"BOOKMOBI"
EOF_RECORD = b"\xe9\x8e\r\n"


def signatures(fp, chunk_size: int = 8 * 1024 * 1024):
    overlap = len(SIGNATURE) - 1
    base = 0
    tail = b""
    while True:
        chunk = fp.read(chunk_size)
        if not chunk:
            break
        data = tail + chunk
        for match in re.finditer(SIGNATURE, data):
            absolute = base - len(tail) + match.start()
            if absolute >= base - len(tail):
                yield absolute
        tail = data[-overlap:]
        base += len(chunk)


def clean_name(raw: bytes, fallback: str) -> str:
    for encoding in ("utf-8", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            pass
    else:
        text = raw.decode("utf-8", "replace")
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", text).strip(" ._")
    return text or fallback


def carve(image: Path, output: Path) -> int:
    output.mkdir(parents=True, exist_ok=True)
    count = 0
    with image.open("rb") as fp:
        sigs = list(signatures(fp))
        for index, signature_at in enumerate(sigs, 1):
            start = signature_at - 60
            if start < 0:
                continue
            fp.seek(start)
            header = fp.read(78)
            if len(header) != 78 or header[60:68] != SIGNATURE:
                continue
            records = struct.unpack_from(">H", header, 76)[0]
            if not 1 <= records <= 10000:
                continue
            table = fp.read(records * 8)
            offsets = [struct.unpack_from(">I", table, pos * 8)[0] for pos in range(records)]
            if not offsets or offsets != sorted(offsets) or offsets[0] < 78 + records * 8:
                continue
            record0 = start + offsets[0]
            fp.seek(record0)
            first = fp.read(512)
            if len(first) < 24 or first[16:20] != b"MOBI":
                continue
            mobi_length = struct.unpack_from(">I", first, 20)[0]
            name_offset = struct.unpack_from(">I", first, 84)[0]
            name_length = struct.unpack_from(">I", first, 88)[0]
            encryption = struct.unpack_from(">H", first, 12)[0]
            fp.seek(record0 + name_offset)
            title_raw = fp.read(min(name_length, 1000))
            title = clean_name(title_raw, f"recovered_mobi_{index}")

            last = start + offsets[-1]
            fp.seek(last)
            last_head = fp.read(64)
            if last_head.startswith(EOF_RECORD):
                end = last + len(EOF_RECORD)
            else:
                # MOBI files are FAT-cluster aligned in this image. If the last
                # record is not the standard EOF record, preserve up to the next
                # detected PalmDB start as a conservative fallback.
                next_start = sigs[index] - 60 if index < len(sigs) else image.stat().st_size
                end = next_start
            size = end - start
            if size <= 0 or size > 2 * 1024 * 1024 * 1024:
                continue
            destination = output / f"{title}.mobi"
            serial = 2
            while destination.exists():
                destination = output / f"{title} ({serial}).mobi"
                serial += 1
            fp.seek(start)
            remaining = size
            with destination.open("wb") as out:
                while remaining:
                    chunk = fp.read(min(8 * 1024 * 1024, remaining))
                    if not chunk:
                        break
                    out.write(chunk)
                    remaining -= len(chunk)
            drm = "DRM/szyfrowany" if encryption else "bez DRM"
            print(f"{destination} | {size} bajtów | {records} rekordów | {drm} | MOBI header {mobi_length}")
            count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(f"Odzyskano: {carve(args.image, args.output)} plików MOBI")


if __name__ == "__main__":
    main()
