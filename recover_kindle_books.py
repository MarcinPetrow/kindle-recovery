#!/usr/bin/env python3
"""Read-only FAT32 extractor for MOBI and EPUB files, including simple undelete."""

from __future__ import annotations

import argparse
import os
import struct
from pathlib import Path


# AZW/PRC files use the same PalmDB/MOBI family of containers. Kindle devices
# commonly store them under these extensions even when the payload is MOBI.
TARGET_EXTENSIONS = {".mobi", ".epub", ".azw", ".azw3", ".azw8", ".prc", ".kfx", ".kcr"}


class Fat32:
    def __init__(self, image: Path):
        self.fp = image.open("rb")
        boot = self.fp.read(512)
        self.bytes_per_sector = struct.unpack_from("<H", boot, 11)[0]
        self.sectors_per_cluster = boot[13]
        self.reserved_sectors = struct.unpack_from("<H", boot, 14)[0]
        self.fat_count = boot[16]
        self.fat_sectors = struct.unpack_from("<I", boot, 36)[0]
        self.root_cluster = struct.unpack_from("<I", boot, 44)[0]
        self.cluster_size = self.bytes_per_sector * self.sectors_per_cluster
        self.fat_offset = self.reserved_sectors * self.bytes_per_sector
        self.data_offset = (
            self.reserved_sectors + self.fat_count * self.fat_sectors
        ) * self.bytes_per_sector

    def cluster_offset(self, cluster: int) -> int:
        return self.data_offset + (cluster - 2) * self.cluster_size

    def read_cluster(self, cluster: int) -> bytes:
        self.fp.seek(self.cluster_offset(cluster))
        return self.fp.read(self.cluster_size)

    def fat_entry(self, cluster: int) -> int:
        self.fp.seek(self.fat_offset + cluster * 4)
        return struct.unpack("<I", self.fp.read(4))[0] & 0x0FFFFFFF

    def chain(self, first: int):
        seen = set()
        current = first
        while 2 <= current < 0x0FFFFFF8 and current not in seen:
            seen.add(current)
            yield current
            current = self.fat_entry(current)

    def read_chain(self, first: int, size: int | None = None) -> bytes:
        chunks = []
        remaining = size
        for cluster in self.chain(first):
            data = self.read_cluster(cluster)
            if remaining is not None:
                data = data[:remaining]
                remaining -= len(data)
            chunks.append(data)
            if remaining is not None and remaining <= 0:
                break
        return b"".join(chunks)

    def read_deleted(self, first: int, size: int) -> bytes:
        # A deleted FAT chain is normally zeroed. Recover its old contiguous
        # allocation only while clusters remain marked free.
        chunks = []
        count = (size + self.cluster_size - 1) // self.cluster_size
        for cluster in range(first, first + count):
            if cluster != first and self.fat_entry(cluster) != 0:
                break
            chunks.append(self.read_cluster(cluster))
        return b"".join(chunks)[:size]


def lfn_piece(entry: bytes) -> str:
    raw = entry[1:11] + entry[14:26] + entry[28:32]
    chars = []
    for index in range(0, len(raw), 2):
        value = struct.unpack_from("<H", raw, index)[0]
        if value in (0, 0xFFFF):
            break
        chars.append(chr(value))
    return "".join(chars)


def short_name(entry: bytes, deleted: bool) -> str:
    base = bytearray(entry[:8])
    if deleted:
        base[0] = ord("_")
    base_text = bytes(base).decode("cp437", "replace").rstrip()
    ext_text = entry[8:11].decode("cp437", "replace").rstrip()
    return base_text + (("." + ext_text) if ext_text else "")


def safe_name(name: str) -> str:
    clean = name.replace("/", "_").replace("\\", "_").replace("\x00", "")
    return clean or "unnamed"


def recover(image: Path, output: Path) -> tuple[int, int]:
    fat = Fat32(image)
    output.mkdir(parents=True, exist_ok=True)
    recovered = 0
    deleted_recovered = 0
    visited_dirs = set()

    def walk(first_cluster: int, relative: Path):
        nonlocal recovered, deleted_recovered
        if first_cluster in visited_dirs:
            return
        visited_dirs.add(first_cluster)
        directory = fat.read_chain(first_cluster)
        lfn = []
        for pos in range(0, len(directory), 32):
            entry = directory[pos : pos + 32]
            if len(entry) < 32 or entry[0] == 0:
                break
            if entry[0] == 0xE5:
                deleted = True
            else:
                deleted = False
            attr = entry[11]
            if attr == 0x0F:
                lfn.append(lfn_piece(entry))
                continue
            if attr & 0x08:
                lfn.clear()
                continue
            # LFN entries occur in reverse order on disk.
            name = "".join(reversed(lfn)) if lfn and not deleted else short_name(entry, deleted)
            lfn.clear()
            cluster = (struct.unpack_from("<H", entry, 20)[0] << 16) | struct.unpack_from("<H", entry, 26)[0]
            size = struct.unpack_from("<I", entry, 28)[0]
            if name in (".", ".."):
                continue
            if attr & 0x10:
                if not deleted and cluster >= 2:
                    walk(cluster, relative / safe_name(name))
                continue
            if Path(name).suffix.lower() not in TARGET_EXTENSIONS or cluster < 2 or size == 0:
                continue
            group = "deleted" if deleted else "existing"
            destination = output / group / relative / safe_name(name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            candidate = destination
            suffix_number = 1
            while candidate.exists():
                candidate = destination.with_name(f"{destination.stem}_{suffix_number}{destination.suffix}")
                suffix_number += 1
            data = fat.read_deleted(cluster, size) if deleted else fat.read_chain(cluster, size)
            if len(data) != size:
                print(f"UWAGA: częściowy plik {name}: {len(data)}/{size} bajtów")
            candidate.write_bytes(data)
            print(f"{'USUNIĘTY' if deleted else 'ISTNIEJĄCY'}: {candidate} ({len(data)} bajtów)")
            recovered += 1
            deleted_recovered += int(deleted)

    walk(fat.root_cluster, Path())
    return recovered, deleted_recovered


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    total, deleted = recover(args.image, args.output)
    print(f"Gotowe: {total} plików, w tym {deleted} oznaczonych jako usunięte.")


if __name__ == "__main__":
    main()
