#!/usr/bin/env python3
"""Read-only EPUB/MOBI recovery tool for Kindle FAT32 disk images."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import struct
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from carve_mobi import carve as carve_mobi
from recover_kindle_books import Fat32, recover as recover_fat


EPUB_PREFIX = b"PK\x03\x04"
EPUB_MIMETYPE = b"application/epub+zip"
EOCD = b"PK\x05\x06"
SCAN_CHUNK = 8 * 1024 * 1024


def human_size(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        while chunk := fp.read(SCAN_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def scan_bytes(fp, needle: bytes, chunk_size: int = SCAN_CHUNK):
    """Yield absolute offsets without loading the whole image into memory."""
    fp.seek(0)
    overlap = len(needle) - 1
    previous = b""
    absolute = 0
    last_result = -1
    while chunk := fp.read(chunk_size):
        data = previous + chunk
        base = absolute - len(previous)
        cursor = 0
        while True:
            found = data.find(needle, cursor)
            if found < 0:
                break
            result = base + found
            if result > last_result:
                yield result
                last_result = result
            cursor = found + 1
        previous = data[-overlap:] if overlap else b""
        absolute += len(chunk)


def fat_info(image: Path) -> dict[str, int | str]:
    with image.open("rb") as fp:
        boot = fp.read(512)
    if len(boot) != 512:
        raise ValueError("obraz jest za mały, aby zawierać sektor startowy")
    bytes_per_sector = struct.unpack_from("<H", boot, 11)[0]
    sectors_per_cluster = boot[13]
    reserved = struct.unpack_from("<H", boot, 14)[0]
    fats = boot[16]
    fat_sectors = struct.unpack_from("<I", boot, 36)[0]
    root_cluster = struct.unpack_from("<I", boot, 44)[0]
    if bytes_per_sector not in (512, 1024, 2048, 4096):
        raise ValueError("nieobsługiwany lub uszkodzony sektor startowy FAT32")
    if not sectors_per_cluster or sectors_per_cluster & (sectors_per_cluster - 1):
        raise ValueError("nieprawidłowy rozmiar klastra FAT32")
    data_offset = (reserved + fats * fat_sectors) * bytes_per_sector
    return {
        "filesystem": "FAT32",
        "label": boot[71:82].decode("ascii", "replace").strip(),
        "image_size": image.stat().st_size,
        "bytes_per_sector": bytes_per_sector,
        "sectors_per_cluster": sectors_per_cluster,
        "cluster_size": bytes_per_sector * sectors_per_cluster,
        "fat_count": fats,
        "fat_sectors": fat_sectors,
        "root_cluster": root_cluster,
        "data_offset": data_offset,
    }


def inspect_image(image: Path) -> dict[str, object]:
    info: dict[str, object] = fat_info(image)
    with image.open("rb") as fp:
        info["bookmobi_signatures"] = sum(1 for _ in scan_bytes(fp, b"BOOKMOBI"))
        info["epub_signatures"] = sum(1 for _ in epub_starts(fp))
    return info


def epub_starts(fp):
    """Find spec-compliant EPUB ZIPs whose first uncompressed file is mimetype."""
    for start in scan_bytes(fp, EPUB_PREFIX):
        fp.seek(start)
        header = fp.read(30)
        if len(header) != 30:
            continue
        try:
            signature, _version, flags, compression, _time, _date, _crc, packed, raw_size, name_len, extra_len = struct.unpack(
                "<IHHHHHIIIHH", header
            )
        except struct.error:
            continue
        if signature != 0x04034B50 or flags & 0x08 or compression != 0:
            continue
        name = fp.read(name_len)
        fp.seek(extra_len, 1)
        content = fp.read(raw_size)
        if name == b"mimetype" and packed == raw_size and content == EPUB_MIMETYPE:
            yield start


def find_epub_end(fp, start: int, image_size: int, max_size: int) -> int | None:
    """Locate and validate the non-ZIP64 end-of-central-directory record."""
    position = start
    limit = min(image_size, start + max_size)
    overlap = b""
    while position < limit:
        fp.seek(position)
        chunk = fp.read(min(SCAN_CHUNK, limit - position))
        if not chunk:
            break
        data = overlap + chunk
        base = position - len(overlap)
        cursor = 0
        while True:
            found = data.find(EOCD, cursor)
            if found < 0:
                break
            eocd_at = base + found
            fp.seek(eocd_at)
            record = fp.read(22)
            if len(record) == 22:
                fields = struct.unpack("<IHHHHIIH", record)
                _, disk, cd_disk, disk_entries, entries, cd_size, cd_offset, comment_size = fields
                end = eocd_at + 22 + comment_size
                central_at = start + cd_offset
                if (
                    disk == cd_disk == 0
                    and disk_entries == entries
                    and central_at + cd_size == eocd_at
                    and end <= image_size
                ):
                    fp.seek(central_at)
                    if fp.read(4) == b"PK\x01\x02":
                        return end
            cursor = found + 1
        overlap = data[-21:]
        position += len(chunk)
    return None


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    number = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
        if not candidate.exists():
            return candidate
        number += 1


def copy_range(fp, start: int, end: int, destination: Path) -> None:
    fp.seek(start)
    remaining = end - start
    with destination.open("wb") as out:
        while remaining:
            chunk = fp.read(min(SCAN_CHUNK, remaining))
            if not chunk:
                raise IOError("nieoczekiwany koniec obrazu")
            out.write(chunk)
            remaining -= len(chunk)


def epub_metadata(path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(path) as archive:
        container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
        rootfile = container.find(".//{*}rootfile")
        if rootfile is None or not rootfile.get("full-path"):
            raise ValueError("brak rootfile w container.xml")
        package = ElementTree.fromstring(archive.read(rootfile.get("full-path")))
        title_node = package.find(".//{*}title")
        author_node = package.find(".//{*}creator")
        title = title_node.text.strip() if title_node is not None and title_node.text else "Odzyskany EPUB"
        author = author_node.text.strip() if author_node is not None and author_node.text else ""
        return title, author


def safe_filename(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value).strip(" .")
    return value[:180] or "recovered"


def carve_epubs(image: Path, output: Path, max_size: int) -> int:
    output.mkdir(parents=True, exist_ok=True)
    image_size = image.stat().st_size
    count = 0
    with image.open("rb") as fp:
        starts = list(epub_starts(fp))
        for number, start in enumerate(starts, 1):
            end = find_epub_end(fp, start, image_size, max_size)
            if end is None:
                print(f"UWAGA: EPUB przy bajcie {start} nie ma wiarygodnego końca", file=sys.stderr)
                continue
            temporary = output / f".epub-{start}.tmp"
            copy_range(fp, start, end, temporary)
            try:
                with zipfile.ZipFile(temporary) as archive:
                    bad = archive.testzip()
                    if bad:
                        raise ValueError(f"błąd CRC w {bad}")
                title, author = epub_metadata(temporary)
                label = f"{author} - {title}" if author else title
                destination = unique_path(output / f"{safe_filename(label)}.epub")
                temporary.replace(destination)
                print(f"EPUB: {destination} | {human_size(destination.stat().st_size)} | offset {start}")
                count += 1
            except Exception as exc:
                temporary.unlink(missing_ok=True)
                print(f"UWAGA: odrzucono EPUB przy bajcie {start}: {exc}", file=sys.stderr)
    return count


def verify_epub(path: Path) -> tuple[bool, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.read("mimetype") != EPUB_MIMETYPE:
                return False, "nieprawidłowy mimetype"
            bad = archive.testzip()
            if bad:
                return False, f"błąd CRC: {bad}"
        title, author = epub_metadata(path)
        return True, f"{author} — {title}" if author else title
    except Exception as exc:
        return False, str(exc)


def palmdoc_decompress(data: bytes, limit: int) -> bytes:
    """Decompress one PalmDOC record, rejecting invalid back-references."""
    output = bytearray()
    position = 0
    while position < len(data) and len(output) < limit:
        code = data[position]
        position += 1
        if 1 <= code <= 8:
            if position + code > len(data):
                raise ValueError("ucięty ciąg literałów PalmDOC")
            output.extend(data[position : position + code])
            position += code
        elif code < 0x80:
            output.append(code)
        elif code >= 0xC0:
            output.extend((0x20, code ^ 0x80))
        else:
            if position >= len(data):
                raise ValueError("ucięte odwołanie PalmDOC")
            pair = ((code & 0x3F) << 8) | data[position]
            position += 1
            distance = pair >> 3
            length = (pair & 7) + 3
            if distance == 0 or distance > len(output):
                raise ValueError("nieprawidłowe odwołanie wsteczne PalmDOC")
            for _ in range(length):
                output.append(output[-distance])
                if len(output) >= limit:
                    break
    return bytes(output[:limit])


def verify_mobi(path: Path) -> tuple[bool, str]:
    try:
        with path.open("rb") as fp:
            header = fp.read(78)
            if len(header) < 78 or header[60:68] != b"BOOKMOBI":
                return False, "brak nagłówka PalmDB/BOOKMOBI"
            records = struct.unpack_from(">H", header, 76)[0]
            table = fp.read(records * 8)
            if len(table) != records * 8:
                return False, "niepełna tablica rekordów"
            offsets = [struct.unpack_from(">I", table, index * 8)[0] for index in range(records)]
            if not offsets or offsets != sorted(offsets) or offsets[-1] >= path.stat().st_size:
                return False, "nieprawidłowe przesunięcia rekordów"
            fp.seek(offsets[0])
            record0 = fp.read(96)
            if len(record0) < 92 or record0[16:20] != b"MOBI":
                return False, "brak wewnętrznego nagłówka MOBI"
            encryption = struct.unpack_from(">H", record0, 12)[0]
            compression = struct.unpack_from(">H", record0, 0)[0]
            text_length = struct.unpack_from(">I", record0, 4)[0]
            text_records = struct.unpack_from(">H", record0, 8)[0]
            record_size = struct.unpack_from(">H", record0, 10)[0]
            if text_records >= records or record_size == 0:
                return False, "nieprawidłowe parametry rekordów tekstowych"
            name_offset = struct.unpack_from(">I", record0, 84)[0]
            name_length = struct.unpack_from(">I", record0, 88)[0]
            fp.seek(offsets[0] + name_offset)
            title = fp.read(min(name_length, 1000)).decode("utf-8", "replace").strip("\x00")
            drm = "DRM" if encryption else "bez DRM"
            if not encryption and compression in (1, 2):
                recovered_text = 0
                broken_records = 0
                for index in range(1, text_records + 1):
                    start = offsets[index]
                    end = offsets[index + 1] if index + 1 < records else path.stat().st_size
                    fp.seek(start)
                    payload = fp.read(end - start)
                    expected = min(record_size, max(0, text_length - recovered_text))
                    try:
                        decoded = payload[:expected] if compression == 1 else palmdoc_decompress(payload, expected)
                    except ValueError:
                        broken_records += 1
                        continue
                    recovered_text += len(decoded)
                if broken_records or recovered_text < text_length:
                    return False, (
                        f"{title}: uszkodzone rekordy tekstu "
                        f"({recovered_text}/{text_length} B, błędne rekordy: {broken_records})"
                    )
            return True, f"{title} ({records} rekordów, {drm})"
    except Exception as exc:
        return False, str(exc)


def import_recovered(sources: list[Path], output: Path) -> tuple[int, int]:
    """Validate and import books produced by PhotoRec or another carver."""
    output.mkdir(parents=True, exist_ok=True)
    known_hashes = {
        sha256(path)
        for path in output.iterdir()
        if path.is_file() and path.suffix.lower() in {".epub", ".mobi", ".azw", ".azw3", ".prc"}
    }
    candidates: list[Path] = []
    for source in sources:
        if source.is_file():
            candidates.append(source)
        elif source.is_dir():
            candidates.extend(
                path
                for path in source.rglob("*")
                if path.is_file() and path.suffix.lower() in {".epub", ".mobi", ".azw", ".azw3", ".prc"}
            )
    imported = 0
    duplicates = 0
    for source in candidates:
        extension = source.suffix.lower()
        if extension == ".epub":
            valid, description = verify_epub(source)
            if not valid:
                print(f"UWAGA: pominięto {source}: {description}", file=sys.stderr)
                continue
            title, author = epub_metadata(source)
            label = f"{author} - {title}" if author else title
            destination = output / f"{safe_filename(label)}.epub"
        else:
            valid, description = verify_mobi(source)
            if not valid:
                print(f"UWAGA: pominięto {source}: {description}", file=sys.stderr)
                continue
            title = description.rsplit(" (", 1)[0]
            destination = output / f"{safe_filename(title)}.mobi"
        fingerprint = sha256(source)
        if fingerprint in known_hashes:
            duplicates += 1
            continue
        destination = unique_path(destination)
        shutil.copy2(source, destination)
        known_hashes.add(fingerprint)
        imported += 1
        print(f"IMPORT: {destination} <- {source}")
    return imported, duplicates


def build_manifest(root: Path) -> dict[str, object]:
    entries = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".epub", ".mobi", ".azw", ".azw3", ".prc"}:
            continue
        if path.suffix.lower() == ".epub":
            valid, description = verify_epub(path)
            kind = "EPUB"
        else:
            valid, description = verify_mobi(path)
            kind = "MOBI"
        entries.append(
            {
                "path": str(path.relative_to(root)),
                "type": kind,
                "size": path.stat().st_size,
                "sha256": sha256(path),
                "valid": valid,
                "description": description,
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root.resolve()),
        "files": entries,
    }


def command_inspect(args) -> int:
    info = inspect_image(args.image)
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        print(f"Obraz: {args.image.resolve()}")
        print(f"Rozmiar: {human_size(int(info['image_size']))}")
        print(f"System plików: {info['filesystem']} ({info['label']})")
        print(f"Klaster: {info['cluster_size']} B; dane od bajtu {info['data_offset']}")
        print(f"Sygnatury BOOKMOBI: {info['bookmobi_signatures']}")
        print(f"Sygnatury EPUB: {info['epub_signatures']}")
    return 0


def command_verify(args) -> int:
    manifest = build_manifest(args.directory)
    destination = args.manifest or args.directory / "manifest.json"
    destination.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    invalid = 0
    for entry in manifest["files"]:
        status = "OK" if entry["valid"] else "BŁĄD"
        print(f"{status}: {entry['path']} — {entry['description']}")
        invalid += not entry["valid"]
    print(f"Manifest: {destination}")
    return 1 if invalid else 0


def command_recover(args) -> int:
    image = args.image.resolve()
    output = args.output.resolve()
    if not image.is_file():
        raise FileNotFoundError(image)
    if output == image.parent or image.is_relative_to(output):
        raise ValueError("katalog wyjściowy musi być oddzielony od obrazu źródłowego")
    fat_info(image)  # Fail before creating output if this is not supported FAT32.
    output.mkdir(parents=True, exist_ok=True)
    books = output / "books"
    books.mkdir(exist_ok=True)

    if not args.skip_fat:
        total, deleted = recover_fat(image, output / "fat")
        print(f"FAT32: {total} plików ({deleted} ze skasowanych wpisów)")
    if not args.skip_epub:
        print(f"EPUB: odzyskano {carve_epubs(image, books, args.max_epub_size)}")
    if not args.skip_mobi:
        print(f"MOBI: odzyskano {carve_mobi(image, books)}")
    if args.photorec_results:
        imported, duplicates = import_recovered(args.photorec_results, books)
        print(f"Import PhotoRec: {imported} nowych, {duplicates} duplikatów")

    verify_args = argparse.Namespace(directory=output, manifest=output / "manifest.json")
    return command_verify(verify_args)


def command_import(args) -> int:
    imported, duplicates = import_recovered(args.sources, args.output)
    print(f"Zaimportowano: {imported}; pominięte duplikaty: {duplicates}")
    verify_args = argparse.Namespace(directory=args.output, manifest=args.output / "manifest.json")
    return command_verify(verify_args)


def positive_size(value: str) -> int:
    size = int(value)
    if size <= 0:
        raise argparse.ArgumentTypeError("wartość musi być dodatnia")
    return size


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(
        description="Odzyskiwanie EPUB/MOBI z obrazu pamięci Kindle (tylko odczyt źródła)."
    )
    main.add_argument("--version", action="version", version="kindle-recover 0.1.0")
    subcommands = main.add_subparsers(dest="command", required=True)

    inspect = subcommands.add_parser("inspect", help="rozpoznaj obraz i policz sygnatury")
    inspect.add_argument("image", type=Path)
    inspect.add_argument("--json", action="store_true")
    inspect.set_defaults(handler=command_inspect)

    recover = subcommands.add_parser("recover", help="odzyskaj książki")
    recover.add_argument("image", type=Path)
    recover.add_argument("-o", "--output", type=Path, default=Path("kindle-recovered"))
    recover.add_argument("--skip-fat", action="store_true", help="pomiń zachowane wpisy FAT32")
    recover.add_argument("--skip-epub", action="store_true", help="pomiń carving EPUB")
    recover.add_argument("--skip-mobi", action="store_true", help="pomiń carving MOBI")
    recover.add_argument(
        "--photorec-results",
        type=Path,
        nargs="+",
        metavar="PATH",
        help="zaimportuj i zweryfikuj książki z podanych katalogów PhotoRec",
    )
    recover.add_argument(
        "--max-epub-size",
        type=positive_size,
        default=2 * 1024 * 1024 * 1024,
        metavar="BYTES",
        help="maksymalny zakres poszukiwania końca EPUB (domyślnie 2 GiB)",
    )
    recover.set_defaults(handler=command_recover)

    verify = subcommands.add_parser("verify", help="sprawdź odzyskane książki")
    verify.add_argument("directory", type=Path)
    verify.add_argument("--manifest", type=Path)
    verify.set_defaults(handler=command_verify)

    importer = subcommands.add_parser("import-photorec", help="zaimportuj wyniki PhotoRec")
    importer.add_argument("sources", type=Path, nargs="+")
    importer.add_argument("-o", "--output", type=Path, required=True)
    importer.set_defaults(handler=command_import)
    return main


def main() -> int:
    args = parser().parse_args()
    try:
        return args.handler(args)
    except (OSError, ValueError, struct.error) as exc:
        print(f"BŁĄD: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
