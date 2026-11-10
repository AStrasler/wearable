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
