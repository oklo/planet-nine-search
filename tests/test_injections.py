from types import SimpleNamespace

import numpy as np
import pytest

from p9search import injections
from p9search.calibration import response_check
from p9search.science.binning import bin_detector_cadences


@pytest.mark.parametrize("minutes", [2, 30])
def test_native_injection_matches_dense_float32_oracle_at_patch_edges(
    tmp_path, monkeypatch, minutes
):
    rng = np.random.default_rng(713)
    nt = 60
    raw = rng.uniform(10, 100000, (nt, 140, 140)).astype(np.float32)
    times = np.arange(nt) * minutes / 1440
    keep = np.ones(nt, bool)
    keep[12:31] = False
    exposure = np.full(nt, minutes / 1440)
    zeros = np.zeros(nt)
    reduced = bin_detector_cadences(raw, np.zeros_like(raw), times, keep, zeros, zeros, exposure)
    np.save(tmp_path / "raw.npy", reduced[0])
    (tmp_path / "strips").mkdir()
    for start in range(0, 140, 4):
        np.save(tmp_path / "strips" / f"{start:03d}-native.npy", raw[:, start : start + 4])
    paths = np.column_stack([np.linspace(-5, 145, nt), np.linspace(136, 3, nt)])
    stamp = np.exp(-np.sum(np.mgrid[-6:7, -6:7] ** 2, axis=0) / 3).astype(np.float32)
    stamp /= stamp.sum()
    monkeypatch.setattr(
        injections,
        "tess_pixel_model",
        lambda x, y, meta, radius: np.repeat(stamp[None], len(x), axis=0),
    )
    sector = SimpleNamespace(
        directory=tmp_path,
        meta={},
        cadences={"native_time": times, "native_keep": keep, "exposure": exposure},
        geom={"time": reduced[2]},
    )
    flux = 1.17
    dense = raw.copy()
    for i, (x, y) in enumerate(np.rint(paths).astype(int)):
        for dy in range(-6, 7):
            for dx in range(-6, 7):
                if 0 <= y + dy < 140 and 0 <= x + dx < 140:
                    dense[i, y + dy, x + dx] += np.float32(stamp[dy + 6, dx + 6] * np.float32(flux))
    expected = bin_detector_cadences(
        dense, np.zeros_like(dense), times, keep, zeros, zeros, exposure
    )[0]
    actual = injections.inject(sector, paths, flux)
    np.testing.assert_array_equal(actual, expected)
    np.testing.assert_array_equal(np.load(tmp_path / "raw.npy"), reduced[0])
    # Adding to binned pixels would miss native rounding and must differ here.
    assert np.any(expected != reduced[0])


def test_source_transfer_failure_cannot_count_as_assessed_recovery():
    raw = np.ones((10, 2, 2), np.float32)
    template = np.full_like(raw, 0.25)
    region = (slice(None), slice(None), slice(None))
    good = np.ones(10, bool)
    before = {"identifiable": True, "flux": 0.1}
    after = {"identifiable": True, "flux": 1.1}
    check = response_check(raw, raw + template, template, region, good, 1.0, before, after)
    assert check["assessed"]
    after["flux"] = 0.7
    assert not response_check(raw, raw + template, template, region, good, 1.0, before, after)[
        "assessed"
    ]
    after["flux"] = 1.1
    assert not response_check(
        raw, raw + template * 1.2, template, region, good, 1.0, before, after
    )["assessed"]


def test_nonfinite_native_pixels_remain_unassessed_and_json_serializable():
    from p9search.storage import canonical

    raw = np.ones((2, 1, 1), np.float32)
    raw[0] = np.nan
    score = {"identifiable": True, "flux": 1.0}
    result = response_check(
        raw, raw, np.ones_like(raw), (slice(None),) * 3, np.ones(2, bool), 1.0, score, score
    )
    assert result["assessed"] is False
    canonical(result)
