from pathlib import Path

import numpy as np

from tools.band_mask import masked_screen
from tools.row_ringing import constrained


def test_mask_and_row_constraint():
    from types import SimpleNamespace

    quality = {"mask": np.zeros((6, 7), bool)}
    sector = SimpleNamespace(meta={"sector": 28})

    def screen(s):
        return {"quality": quality, "weights": (None, np.ones((6, 7)))}

    result = masked_screen(screen, {"28": [[1, -200, 3, 7]]}, 1)(sector)
    assert result["masked_fraction"] == 0.5
    assert not quality["mask"].any()
    assert np.all(result["quality"]["mask"][2:5])
    rows = np.arange(6) == 3
    base = constrained((6, 7), rows).reshape(6, 7, -1)
    quad = np.linalg.qr(np.vander(np.arange(7, dtype=float), 3, increasing=True))[0][:, 2]
    np.testing.assert_allclose(quad @ base[3], 0, atol=1e-7)
    assert np.linalg.norm(quad @ base[2]) > 0.9


def test_time_shifts_preserve_good_exposures_and_downlink_gaps(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    from field_calibration import shifts

    times = np.r_[np.arange(12) / 48.0, 5 + np.arange(12) / 48.0]
    keep = np.arange(24) != 3
    result = shifts(times, keep, np.random.default_rng(12))
    assert result[3] == 3
    assert set(result[:12]) == set(range(12))
    assert set(result[12:]) == set(range(12, 24))
