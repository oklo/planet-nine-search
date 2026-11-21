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


@pytest.mark.parametrize("fraction", [0.05, 0.2, 0.5])
def test_large_masks_preserve_usable_source_information(tmp_path, monkeypatch, fraction):
    from p9search import foreground

    rng = np.random.default_rng(2031)
    raw = rng.normal(100, 1, (80, 40, 40)).astype(np.float32)
    np.save(tmp_path / "raw.npy", raw)
    np.save(tmp_path / "error.npy", np.ones_like(raw))
    np.savez(tmp_path / "stars.npz", xy=[[20.1, 19.8]], tmag=[17.0], flux=[100.0])
    sector = SimpleNamespace(
        directory=tmp_path,
        geom={"time": np.arange(80) * 0.2, "keep": np.ones(80, bool)},
        meta={},
    )
    mask = np.zeros((40, 40), bool)
    mask[: int(40 * fraction)] = True
    monkeypatch.setattr(foreground, "weighted_persistent_steps", lambda *a, **k: {"mask": mask})
    baseline = fit(sector)
    assert baseline["masked_fraction"] == pytest.approx(fraction)
    diagnostic = screen(sector)["difficulty"]
    assert diagnostic["usable_pixel_fraction"] == pytest.approx(1 - fraction)
    assert 100 * fraction - 1e-12 <= diagnostic["score"] <= 100
    assert diagnostic["residual_noise_ratio"] > 0
    assert np.all(baseline["scene"].pixel_weight[mask] == 0)
    assert np.all(baseline["scene"].pixel_weight[~mask] > 0)
    template = np.zeros_like(raw)
    template[np.arange(80), 30, 5 + np.arange(80) % 25] = 1
    before = baseline["projector"].score(template)
    injected = fit(sector, raw_override=raw + template, fixed_weights=baseline["weights"])
    after = injected["projector"].score(template)
    assert before["identifiable"] and after["identifiable"]
    assert 0.9 <= after["flux"] - before["flux"] <= 1.1
    hidden = np.zeros_like(raw)
    hidden[np.arange(80), 0, 5 + np.arange(80) % 25] = 1
    assert not baseline["projector"].score(hidden)["identifiable"]


def test_no_usable_pixels_is_explicit(tmp_path, monkeypatch):
    from p9search import foreground

    rng = np.random.default_rng(2001)
    raw = rng.normal(100, 1, (80, 32, 32)).astype(np.float32)
    np.save(tmp_path / "raw.npy", raw)
    np.save(tmp_path / "error.npy", np.ones_like(raw))
    np.savez(tmp_path / "stars.npz", xy=[[16.1, 15.8]], tmag=[17.0], flux=[100.0])
    sector = SimpleNamespace(
        directory=tmp_path,
        geom={"time": np.arange(80) * 0.2, "keep": np.ones(80, bool)},
        meta={},
    )
    monkeypatch.setattr(
        foreground, "weighted_persistent_steps", lambda *a, **k: {"mask": np.ones((32, 32), bool)}
    )
    with pytest.raises(foreground.QualityRejected, match="No usable pixels") as error:
        screen(sector)
    assert error.value.fraction == 1
