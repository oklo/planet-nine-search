import struct

import numpy as np
import pytest

from p9search.archive import decode_row


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
