import io
import zipfile

import pytest

from p9search.archives import EvidenceStore


def test_archive_roundtrip_deduplicates_bundle_members_and_preserves_bytes(tmp_path):
    source = tmp_path / "field"
    source.mkdir()
    data = b"catalogue\n" * 10000
    (source / "stars.npz").write_bytes(data)
    (source / "result.json").write_bytes(b'{ "result": 1.0 }\n')
    bundle = source / "field.zip"
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("stars.npz", data)
    (source / "raw.npy").write_bytes(b"regenerable pixels")
    with EvidenceStore(tmp_path / "evidence.sqlite") as store:
        record = store.add(
            "field1", {"worker": source}, exclude=[source / "raw.npy"], bundles=[bundle]
        )
        assert len(record["files"]) == 3
        store.restore("field1", tmp_path / "restored")
        for name in ["stars.npz", "result.json", "field.zip"]:
            assert (tmp_path / "restored/worker" / name).read_bytes() == (
                source / name
            ).read_bytes()
        assert not (tmp_path / "restored/worker/raw.npy").exists()
        size = store.db.execute("SELECT SUM(length(payload)) FROM objects").fetchone()[0]
        assert size < 1000
        store.add("field2", {"worker": source}, exclude=[source / "raw.npy"], bundles=[bundle])
        assert store.db.execute("SELECT SUM(length(payload)) FROM objects").fetchone()[0] == size


def test_corruption_blocks_restore_and_archive_is_immutable(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "result.json").write_text("evidence")
    with EvidenceStore(tmp_path / "store.sqlite") as store:
        first = store.add("a", {"worker": source})
        (source / "result.json").write_text("changed source")
        assert store.add("a", {"worker": source}) == first
        store.db.execute("UPDATE objects SET payload=?", (b"corrupt",))
        with pytest.raises(ValueError):
            store.restore("a", tmp_path / "restore")
        assert not (tmp_path / "restore").exists()


def test_archive_rejects_redirected_inputs_and_restore_targets(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "outside").symlink_to(tmp_path / "secret")
    with EvidenceStore(tmp_path / "store.sqlite") as store:
        with pytest.raises(ValueError, match="ordinary"):
            store.add("a", {"worker": source})


def test_bundle_expansion_is_bounded(tmp_path, monkeypatch):
    from p9search import archives

    monkeypatch.setattr(archives, "MAX_FILE", 1024)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("oversized", b"x" * (archives.MAX_FILE + 1))
    with EvidenceStore(tmp_path / "store.sqlite") as store:
        with pytest.raises(ValueError, match="size"):
            store.bundle(stream.getvalue())
