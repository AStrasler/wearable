"""Raw batch archival: newline-delimited frame hex, zstd-compressed, content-addressed.
Pure filesystem — no DB. One file per batch under <root>/<device>/<day>/<batch>.zst."""
import errno
import hashlib
import ntpath
import os
import stat
import uuid
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


def _relative_archive_path(root: str, file_path: str) -> tuple[Path, Path]:
    # Older database rows can retain paths spelled through a symlinked root
    # (for example, /tmp on macOS). Map that root prefix onto its canonical
    # location, but keep the remaining components lexical so secure openat
    # checks can still reject symlinked descendants.
    configured_root = Path(os.path.abspath(os.fspath(root)))
    base = configured_root.resolve()
    candidate = Path(os.path.abspath(os.fspath(file_path)))
    try:
        relative = candidate.relative_to(configured_root)
    except ValueError:
        try:
            relative = candidate.relative_to(base)
        except ValueError as exc:
            raise InvalidArchivePath("archive path is outside the configured root") from exc
    if not relative.parts:
        raise InvalidArchivePath("archive path is outside the configured root")
    return base, relative


def contained_archive_path(root: str, file_path: str) -> str:
    """Return a canonical in-root path, rejecting existing symlinks in its path."""
    base, relative = _relative_archive_path(root, file_path)
    target = base
    for component in relative.parts:
        target = target / component
        if target.is_symlink():
            raise InvalidArchivePath("symlinked archive paths are not allowed")
    return str(target)


def _supports_secure_dir_fd() -> bool:
    return (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and os.replace in os.supports_dir_fd
    )


def _open_directory_chain(root_fd: int, components: tuple[str, ...], *, create: bool) -> int:
    """Open each directory relative to a held fd, refusing symlink traversal."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    current_fd = os.dup(root_fd)
    try:
        for component in components:
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise InvalidArchivePath("archive directory does not exist")
                try:
                    os.mkdir(component, 0o777, dir_fd=current_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise InvalidArchivePath("symlinked archive paths are not allowed") from exc
                raise
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _write_archive_with_dir_fd(base: Path, device_id: str, day: str,
                               filename: str, compressed: bytes) -> str:
    base.mkdir(parents=True, exist_ok=True)
    root_fd = os.open(
        str(base),
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        directory_fd = _open_directory_chain(root_fd, (device_id, day), create=True)
    finally:
        os.close(root_fd)

    temp_name = f".{filename}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        file_fd = os.open(temp_name, flags, 0o666, dir_fd=directory_fd)
        with os.fdopen(file_fd, "wb") as handle:
            handle.write(compressed)

        try:
            existing = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(existing.st_mode):
                raise InvalidArchivePath("archive destination is not a regular file")

        # Rename is relative to the held directory, so a replaced ancestor or a
        # final-component symlink cannot redirect the write outside the archive.
        os.replace(temp_name, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    except Exception:
        try:
            os.unlink(temp_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(directory_fd)

    return str(base / device_id / day / filename)


def _write_archive_portable(base: Path, device_id: str, day: str,
                            filename: str, compressed: bytes) -> str:
    """Portable fallback for platforms without dir_fd/O_NOFOLLOW support."""
    target_dir = base / device_id / day
    current = base
    for component in (device_id, day):
        current = current / component
        if current.is_symlink():
            raise InvalidArchivePath("symlinked archive paths are not allowed")
        current.mkdir(exist_ok=True)
        if current.is_symlink():
            raise InvalidArchivePath("symlinked archive paths are not allowed")

    target = target_dir / filename
    if target.is_symlink():
        raise InvalidArchivePath("symlinked archive paths are not allowed")
    temp = target_dir / f".{filename}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temp, "xb") as handle:
            handle.write(compressed)
        if target.is_symlink():
            raise InvalidArchivePath("symlinked archive paths are not allowed")
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()
    return str(target)


def open_contained_archive(root: str, file_path: str):
    """Open a stored archive without following symlinks beneath the configured root."""
    base, relative = _relative_archive_path(root, file_path)
    if _supports_secure_dir_fd():
        root_fd = os.open(
            str(base),
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            directory_fd = _open_directory_chain(root_fd, relative.parts[:-1], create=False)
        finally:
            os.close(root_fd)
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            file_fd = os.open(relative.parts[-1], flags, dir_fd=directory_fd)
        except OSError as exc:
            os.close(directory_fd)
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise InvalidArchivePath("symlinked archive paths are not allowed") from exc
            raise
        os.close(directory_fd)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            os.close(file_fd)
            raise InvalidArchivePath("archive path is not a regular file")
        return os.fdopen(file_fd, "rb")

    return open(contained_archive_path(root, file_path), "rb")


def write_raw_batch(root: str, device_id: str, batch_id: str, day: str,
                    frames: list[bytes]) -> dict:
    """Write frames (as newline-delimited hex) to a zstd file. Returns metadata for the
    raw_batches index: file_path, sha256, byte_size, packet_count."""
    for component in (device_id, batch_id, day):
        validate_component(component)
    base = Path(root).resolve()
    filename = f"{batch_id}.zst"
    payload = ("\n".join(frame.hex() for frame in frames)).encode()
    compressed = zstandard.ZstdCompressor(level=10).compress(payload)

    if _supports_secure_dir_fd():
        file_path = _write_archive_with_dir_fd(base, device_id, day, filename, compressed)
    else:
        base.mkdir(parents=True, exist_ok=True)
        file_path = _write_archive_portable(base, device_id, day, filename, compressed)

    return {
        "file_path": file_path,
        "sha256": hashlib.sha256(compressed).hexdigest(),
        "byte_size": len(compressed),
        "packet_count": len(frames),
    }
