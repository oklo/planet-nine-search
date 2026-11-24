import numpy as np

from p9search.science.photometry import bright_axes


def test_external_star_row_crosses_track_but_its_column_does_not():
    track = np.array([[10, 20], [20, 21], [151, 21]])
    keep = np.ones(3, dtype=bool)
    assert bright_axes(track, keep, {}) == {}
    result = bright_axes(track, keep, {"gaia_dr3_bright_axes": [[123, -90, 21, 6.5]]})[
        "bright_axes"
    ]
    assert result["in_stamp_epochs"] == 2
    assert result["column"]["pixels"] == 100 and result["row"]["pixels"] == 0
    assert 5 < result["angle_from_row_deg"] < 6
