"""Preflight reuse preserves the baseline fit and injection weight contract."""

from types import SimpleNamespace

import numpy as np
import pytest

from p9search.foreground import fit, screen


def test_preflight_reuse_preserves_noise_weights_residuals_and_scores(tmp_path):
    rng = np.random.default_rng(2001)
    raw = rng.normal(100, 1, (80, 32, 32)).astype(np.float32)
    np.save(tmp_path / "raw.npy", raw)
    np.save(tmp_path / "error.npy", np.ones_like(raw))
    # A faint catalog source keeps this regression independent of PRF downloads.
    np.savez(tmp_path / "stars.npz", xy=[[16.1, 15.8]], tmag=[17.0], flux=[100.0])
    sector = SimpleNamespace(
        directory=tmp_path,
        geom={"time": np.arange(80) * 0.2, "keep": np.ones(80, bool)},
        meta={},
    )
    direct = fit(sector)
    quality = screen(sector)
    reused = fit(sector, screened=quality)
    for a, b in zip(direct["weights"], reused["weights"], strict=True):
        np.testing.assert_array_equal(a, b)
    np.testing.assert_array_equal(direct["scene"].residual, reused["scene"].residual)
    assert direct["masked_fraction"] == reused["masked_fraction"]
    template = rng.normal(size=raw.shape)
    assert direct["projector"].score(template) == reused["projector"].score(template)
    with pytest.raises(ValueError, match="unchanged baseline"):
        fit(sector, raw_override=raw + 1, fixed_weights=reused["weights"], screened=quality)
    with pytest.raises(ValueError, match="baseline noise weights"):
        fit(sector, raw_override=raw + 1)
