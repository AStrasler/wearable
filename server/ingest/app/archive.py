"""Raw batch archival: newline-delimited frame hex, zstd-compressed, content-addressed.
Pure filesystem — no DB. One file per batch under <root>/<device>/<day>/<batch>.zst."""
import hashlib
import ntpath
import os
from pathlib import Path

import zstandard


class InvalidArchivePath(ValueError):
    """An archive identifier or stored path escapes the archive boundary."""


def validate_component(value: str) -> None:
    """Archive identifiers are opaque basenames, never paths."""
    if (not isinstance(value, str) or not value or value in {".", ".."}
            or any(char in value for char in ("/", "\\", "\0"))
            or ntpath.splitdrive(value)[0]
            or (os.name == "nt" and ":" in value)):
        raise InvalidArchivePath("invalid archive identifier")


def contained_archive_path(root: str, file_path: str) -> str:
    """Resolve existing symlinks before accepting any archive file access."""
    base = Path(root).resolve()
    target = Path(file_path).resolve()
    if not target.is_relative_to(base) or target == base:
        raise InvalidArchivePath("archive path is outside the configured root")
    return str(target)


def write_raw_batch(root: str, device_id: str, batch_id: str, day: str,
                    frames: list[bytes]) -> dict:
    """Write frames (as newline-delimited hex) to a zstd file. Returns metadata for the
    raw_batches index: file_path, sha256, byte_size, packet_count."""
    for component in (device_id, batch_id, day):
        validate_component(component)
    file_path = contained_archive_path(root, os.path.join(root, device_id, day, f"{batch_id}.zst"))
    out_dir = os.path.dirname(file_path)
    os.makedirs(out_dir, exist_ok=True)
    payload = ("\n".join(f.hex() for f in frames)).encode()
    compressed = zstandard.ZstdCompressor(level=10).compress(payload)
    # atomic-ish write
    tmp = contained_archive_path(root, file_path + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(compressed)
    os.replace(tmp, file_path)
    return {
        "file_path": file_path,
        "sha256": hashlib.sha256(compressed).hexdigest(),
        "byte_size": len(compressed),
        "packet_count": len(frames),
    }
