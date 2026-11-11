from pathlib import Path

import pytest
import zstandard

from app.archive import InvalidArchivePath, contained_archive_path, write_raw_batch


@pytest.mark.parametrize("component", ["", ".", "..", "../escape", "/absolute", "a/b", "a\\b", "C:\\escape", "C:escape", "\\\\host\\share", "bad\0id"])
@pytest.mark.parametrize("field", ["device_id", "batch_id", "day"])
def test_rejects_path_components_before_writing(tmp_path, component, field):
    args = {"device_id": "devA", "batch_id": "b1", "day": "2026-05-23"}
    args[field] = component
    with pytest.raises(InvalidArchivePath):
        write_raw_batch(str(tmp_path), frames=[b"\xaa\x00"], **args)
    assert list(tmp_path.iterdir()) == []


def test_preserves_opaque_archive_ids_and_roundtrip(tmp_path):
    root = tmp_path / "raw"
    result = write_raw_batch(str(root), "dev-A_1", "hist-dev-A_1-123", "2026-05-23", [b"\xaa\x00"])
    path = contained_archive_path(str(root), result["file_path"])
    with open(path, "rb") as handle:
        assert zstandard.ZstdDecompressor().decompress(handle.read()) == b"aa00"
    assert result["packet_count"] == 1


def test_rejects_stored_external_path_and_prefix_sibling(tmp_path):
    from app.read import read_batch_frames

    root = tmp_path / "raw"
    for target in [tmp_path / "secret.zst", tmp_path / "raw-other" / "batch.zst", root]:
        with pytest.raises(InvalidArchivePath):
            contained_archive_path(str(root), str(target))
        with pytest.raises(InvalidArchivePath):
            read_batch_frames(str(target), raw_root=str(root))


def test_accepts_legacy_path_through_symlinked_root(tmp_path):
    from app.archive import open_contained_archive

    canonical_root = tmp_path / "canonical-raw"
    configured_root = tmp_path / "raw-link"
    result = write_raw_batch(str(canonical_root), "devA", "batch", "2026-05-23", [b"\xaa\x00"])
    try:
        configured_root.symlink_to(canonical_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    # Simulate a persisted path recorded before the configured root was resolved.
    legacy_path = configured_root / "devA" / "2026-05-23" / "batch.zst"
    assert contained_archive_path(str(configured_root), str(legacy_path)) == result["file_path"]
    with open_contained_archive(str(configured_root), str(legacy_path)) as archive:
        assert archive.read()


def test_read_requires_archive_root(tmp_path):
    from app.read import read_batch_frames

    with pytest.raises(TypeError, match="raw_root"):
        read_batch_frames(str(tmp_path / "outside.zst"))  # pylint: disable=missing-kwoa


def test_rejects_in_root_symlink_for_write_and_read(tmp_path):
    from app.read import read_batch_frames

    root = tmp_path / "raw"
    target_root = root / "real-device"
    result = write_raw_batch(str(root), "real-device", "batch", "2026-05-23", [b"\xaa\x00"])
    original = Path(result["file_path"]).read_bytes()
    try:
        (root / "alias").symlink_to(target_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(InvalidArchivePath):
        write_raw_batch(str(root), "alias", "batch", "2026-05-23", [b"\xbb"])
    with pytest.raises(InvalidArchivePath):
        read_batch_frames(str(root / "alias" / "2026-05-23" / "batch.zst"), raw_root=str(root))
    assert Path(result["file_path"]).read_bytes() == original


def test_invalid_ids_are_rejected_before_device_or_dedupe_writes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app import ingest

    calls = []
    monkeypatch.setattr(ingest.store, "ensure_device", lambda *args, **kwargs: calls.append("device"))
    monkeypatch.setattr(ingest.store, "batch_exists", lambda *args, **kwargs: calls.append("dedupe") or True)

    for device_id, batch_id in [("../escape", "b1"), ("devA", "../escape")]:
        batch = {
            "batch_id": batch_id,
            "device": {"device_id": device_id},
            "clock_ref": {"device": 1, "wall": 1},
            "frames": [],
        }
        with pytest.raises(InvalidArchivePath):
            ingest.process_batch(None, SimpleNamespace(raw_root=str(tmp_path)), batch)
    assert calls == []


def test_rejects_existing_symlink_escape(tmp_path):
    root = tmp_path / "raw"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        (root / "device").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    with pytest.raises(InvalidArchivePath):
        write_raw_batch(str(root), "device", "batch", "2026-05-23", [b"\xaa\x00"])
    assert list(outside.iterdir()) == []
