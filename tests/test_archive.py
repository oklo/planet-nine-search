import struct

import numpy as np
import pytest

from p9search.archive import assemble_strips, decode_row


def test_numpy_decoder_matches_independent_struct_reference():
    values = np.array(
        [0.0, -0.0, 1.0, -1.0, 1e20, 1e-20, np.inf, -np.inf, np.nan, 1.125, 42.0, -99.0],
        dtype=">f4",
    )
    wire = values.tobytes()
    expected = np.asarray(struct.unpack(">12f", wire)).reshape(2, 3, 2)
    actual = decode_row(wire, 2, 3, 2)
    assert actual.dtype == np.float64
    np.testing.assert_array_equal(actual, expected)
    np.testing.assert_array_equal(np.signbit(actual), np.signbit(expected))


def test_corrupted_or_misdeclared_row_is_rejected():
    with pytest.raises(ValueError):
        decode_row(bytes(47), 2, 3, 2)
    with pytest.raises(ValueError):
        decode_row(bytes(48), 0, 3, 2)


def test_sequential_assembly_preserves_exact_npy_bytes_across_blocks(tmp_path):
    # Include signed zero, infinities and distinct NaN payloads, without arithmetic.
    bits = np.random.default_rng(42).integers(0, 2**32, (257, 140, 140), dtype=np.uint32)
    bits[0, 0, :5] = [0, 0x80000000, 0x7F800000, 0xFF800000, 0x7FC00001]
    expected = bits.view(np.float32)
    (tmp_path / "strips").mkdir()
    for row in range(0, 140, 4):
        np.save(tmp_path / "strips" / f"{row:03d}-raw.npy", expected[:, row : row + 4])
    reference = tmp_path / "reference.npy"
    np.save(reference, expected)
    (tmp_path / "raw.partial.npy").write_bytes(b"interrupted prior assembly")
    actual = assemble_strips(tmp_path, "raw", len(expected))
    assert actual.read_bytes() == reference.read_bytes()
    assert not (tmp_path / "raw.partial.npy").exists()
    np.save(tmp_path / "strips/136-raw.npy", expected[:, :3])
    with pytest.raises(ValueError, match="strip layout"):
        assemble_strips(tmp_path, "raw", len(expected))
    assert actual.read_bytes() == reference.read_bytes()
