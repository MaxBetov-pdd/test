"""Build a static trust cache without the Darwin-only trustcache binary."""

from __future__ import annotations

import hashlib
import io
import struct
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


CSMAGIC_EMBEDDED_SIGNATURE = 0xFADE0CC0
CSMAGIC_CODEDIRECTORY = 0xFADE0C02
LC_CODE_SIGNATURE = 0x1D
MACHO_MAGICS = {
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xfe\xed\xfa\xce",
    b"\xca\xfe\xba\xbe",
    b"\xca\xfe\xba\xbf",
}


class TrustCacheError(RuntimeError):
    pass


@dataclass(frozen=True, order=True)
class CdHash:
    value: bytes
    hash_type: int


def _macho_slices(data: bytes) -> list[bytes]:
    if len(data) < 8:
        return []
    magic_be = struct.unpack_from(">I", data, 0)[0]
    if magic_be not in {0xCAFEBABE, 0xCAFEBABF}:
        return [data]
    is_64 = magic_be == 0xCAFEBABF
    count = struct.unpack_from(">I", data, 4)[0]
    entry_size = 32 if is_64 else 20
    result: list[bytes] = []
    for index in range(count):
        offset = 8 + index * entry_size
        if offset + entry_size > len(data):
            raise TrustCacheError("Truncated FAT Mach-O header")
        if is_64:
            _cpu, _sub, slice_offset, slice_size, _align, _reserved = struct.unpack_from(">IIQQII", data, offset)
        else:
            _cpu, _sub, slice_offset, slice_size, _align = struct.unpack_from(">IIIII", data, offset)
        end = slice_offset + slice_size
        if end > len(data):
            raise TrustCacheError("FAT Mach-O slice extends past the file")
        result.append(data[slice_offset:end])
    return result


def _code_signature_blob(macho: bytes) -> bytes | None:
    if len(macho) < 28:
        return None
    magic = macho[:4]
    if magic == b"\xcf\xfa\xed\xfe":
        endian, header_size = "<", 32
    elif magic == b"\xce\xfa\xed\xfe":
        endian, header_size = "<", 28
    elif magic == b"\xfe\xed\xfa\xcf":
        endian, header_size = ">", 32
    elif magic == b"\xfe\xed\xfa\xce":
        endian, header_size = ">", 28
    else:
        return None
    ncmds = struct.unpack_from(endian + "I", macho, 16)[0]
    offset = header_size
    for _ in range(ncmds):
        if offset + 8 > len(macho):
            raise TrustCacheError("Truncated Mach-O load commands")
        command, size = struct.unpack_from(endian + "II", macho, offset)
        if size < 8 or offset + size > len(macho):
            raise TrustCacheError("Invalid Mach-O load command size")
        if (command & 0x7FFFFFFF) == LC_CODE_SIGNATURE:
            if size < 16:
                raise TrustCacheError("Truncated LC_CODE_SIGNATURE")
            data_offset, data_size = struct.unpack_from(endian + "II", macho, offset + 8)
            if data_offset + data_size > len(macho):
                raise TrustCacheError("Code signature extends past Mach-O slice")
            return macho[data_offset : data_offset + data_size]
        offset += size
    return None


def _code_directories(superblob: bytes) -> list[bytes]:
    if len(superblob) < 12:
        return []
    magic, length, count = struct.unpack_from(">III", superblob, 0)
    if magic == CSMAGIC_CODEDIRECTORY:
        return [superblob[:length]] if length <= len(superblob) else []
    if magic != CSMAGIC_EMBEDDED_SIGNATURE or length > len(superblob):
        return []
    result: list[bytes] = []
    for index in range(count):
        entry = 12 + index * 8
        if entry + 8 > length:
            raise TrustCacheError("Truncated code-signature SuperBlob index")
        slot, offset = struct.unpack_from(">II", superblob, entry)
        if slot != 0 and not 0x1000 <= slot <= 0x1005:
            continue
        if offset + 8 > length:
            continue
        blob_magic, blob_length = struct.unpack_from(">II", superblob, offset)
        if blob_magic == CSMAGIC_CODEDIRECTORY and offset + blob_length <= length:
            result.append(superblob[offset : offset + blob_length])
    return result


def cdhashes_for_macho(data: bytes) -> list[CdHash]:
    hashes: set[CdHash] = set()
    digest_names = {1: "sha1", 2: "sha256", 3: "sha256", 4: "sha384"}
    for macho in _macho_slices(data):
        signature = _code_signature_blob(macho)
        if signature is None:
            continue
        for directory in _code_directories(signature):
            if len(directory) < 40:
                continue
            hash_type = directory[37]
            digest_name = digest_names.get(hash_type)
            if digest_name is None:
                continue
            digest = hashlib.new(digest_name, directory).digest()[:20]
            hashes.add(CdHash(digest, hash_type))
    return sorted(hashes)


def _payload_member(path_line: str) -> str:
    normalized = path_line.strip().replace("\\", "/")
    marker = "work/sshtar/"
    if marker in normalized:
        normalized = normalized.split(marker, 1)[1]
    return str(PurePosixPath(normalized.lstrip("./")))


def cdhashes_from_payload(payload_tar: Path, list_path: Path) -> list[CdHash]:
    wanted = [
        _payload_member(line)
        for line in list_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    result: set[CdHash] = set()
    with tarfile.open(payload_tar, "r:*") as archive:
        members = {member.name.lstrip("./"): member for member in archive.getmembers()}
        for name in wanted:
            member = members.get(name)
            if member is None:
                raise TrustCacheError(f"SSH payload does not contain listed file: {name}")
            handle = archive.extractfile(member)
            if handle is None:
                raise TrustCacheError(f"Listed trustcache path is not a regular file: {name}")
            data = handle.read()
            hashes = cdhashes_for_macho(data)
            if not hashes and data[:4] in MACHO_MAGICS:
                raise TrustCacheError(f"No Mach-O CodeDirectory found in: {name}")
            result.update(hashes)
    return sorted(result)


def append_hashes(stock: bytes, hashes: list[CdHash]) -> bytes:
    if len(stock) < 24:
        raise TrustCacheError("Stock trust cache is shorter than its 24-byte header")
    version = struct.unpack_from("<I", stock, 0)[0]
    count = struct.unpack_from("<I", stock, 20)[0]
    payload_size = len(stock) - 24
    if count:
        if payload_size % count:
            raise TrustCacheError(
                f"Cannot infer trust-cache entry size: {payload_size} bytes for {count} entries"
            )
        entry_size = payload_size // count
    else:
        entry_size = {0: 20, 1: 22, 2: 24}.get(version, 22)
    if entry_size not in {20, 22, 24}:
        raise TrustCacheError(f"Unsupported trust-cache v{version} entry size: {entry_size}")

    entries = [stock[24 + i * entry_size : 24 + (i + 1) * entry_size] for i in range(count)]
    existing = {(entry[:20], entry[20] if entry_size >= 22 else 0) for entry in entries}
    for item in hashes:
        key = (item.value, item.hash_type if entry_size >= 22 else 0)
        if key in existing:
            continue
        if entry_size == 20:
            entry = item.value
        elif entry_size == 22:
            entry = item.value + bytes((item.hash_type, 0))
        else:
            entry = item.value + bytes((item.hash_type, 0, 0, 0))
        entries.append(entry)
        existing.add(key)
    entries.sort(key=lambda entry: (entry[:20], entry[20:]))
    header = bytearray(stock[:24])
    struct.pack_into("<I", header, 20, len(entries))
    return bytes(header) + b"".join(entries)


def build_trustcache(stock: bytes, payload_tar: Path, list_path: Path) -> tuple[bytes, int]:
    hashes = cdhashes_from_payload(payload_tar, list_path)
    return append_hashes(stock, hashes), len(hashes)
