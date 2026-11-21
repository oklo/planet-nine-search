"""Fit the same Gaia scene and temporal covariance to data and source models."""

import numpy as np

from .science.adaptive_covariance import AdaptiveTemporalProjector
from .science.catalog_covariance import catalog_features
from .science.catalog_star_scene import stellar_columns
from .science.composed_scoring import ComposedTemporalScorer
from .science.continuous_catalog_covariance import ContinuousCatalogVariancePlan
from .science.detector_background import detector_background_columns
from .science.noise_weighted_temporal_basis import learn_noise_weighted_basis
from .science.preprocessing import gaia_design, linear_scene_subtract, preprocess
from .science.scene_likelihood import SceneProjector, scene_tangents, separable_weights
from .science.sparse_composed import SparseComposedScorer
from .science.temporal_pixel_quality_weighted import weighted_persistent_steps

QUALITY_METHOD = "usable-pixels-v1"


class QualityRejected(RuntimeError):
    def __init__(self, fraction):
        super().__init__("No usable pixels remain after masking")
        self.fraction = fraction


def difficulty(scene, mask, keep, star_magnitudes):
    """A scheduling diagnostic, not a detection probability (larger is harder)."""
    v, u = scene.pixel_weight, scene.time_weight
    valid = (v > 0) & ~mask
    pixels = np.flatnonzero(valid)
    epochs = np.flatnonzero(np.asarray(keep, bool) & (u > 0))
    selected_pixels = pixels[np.linspace(0, len(pixels) - 1, min(len(pixels), 1024), dtype=int)]
    selected_epochs = epochs[np.linspace(0, len(epochs) - 1, min(len(epochs), 128), dtype=int)]
    residual = scene.residual.reshape(len(u), -1)[np.ix_(selected_epochs, selected_pixels)]
    normalized = residual * np.sqrt(u[selected_epochs, None] * v.ravel()[selected_pixels])
    noise = float(1.4826 * np.median(abs(normalized - np.median(normalized, axis=0))))
    spatial = float(valid.mean())
    temporal = float(len(epochs) / len(u))
    return {
        "method": "usable-area-exposure-noise-v1",
        "score": float(100 * (1 - spatial * temporal / max(1.0, noise) ** 2)),
        "usable_pixel_fraction": spatial,
        "usable_exposure_fraction": temporal,
        "residual_noise_ratio": noise,
        "bright_stars": int(np.count_nonzero(np.asarray(star_magnitudes) < 15)),
    }


def screen(sector, raw_override=None, fixed_weights=None, *, empirical_stars=False):
    geom = sector.geom
    with np.load(sector.directory / "stars.npz") as archive:
        stars = dict(archive)
    raw = (
        np.load(sector.directory / "raw.npy", mmap_mode="r")
        if raw_override is None
        else raw_override
    )
    gaia = gaia_design(stars["xy"], stars["flux"], raw.shape[1:])
    if fixed_weights is None:
        if raw_override is not None:
            raise ValueError("Injections must retain the baseline noise weights")
        error = np.load(sector.directory / "error.npy", mmap_mode="r")
        residual, weights, _ = preprocess(raw, error, geom["time"], geom["keep"], gaia, "gaia")
        del residual, error
        u, v = separable_weights(weights, geom["keep"])
        del weights
        v = np.where(np.isfinite(raw[u > 0]).all(axis=0), v, 0.0)
    else:
        u, v = fixed_weights
    if not np.any(v > 0):
        raise QualityRejected(1.0)
    corrected, _ = linear_scene_subtract(raw, geom["keep"], gaia)
    selected = (
        np.isfinite(stars["tmag"]) & (stars["tmag"] < 12.0)
        if empirical_stars
        else np.ones(len(stars["tmag"]), dtype=bool)
    )
    tangent, _ = scene_tangents(
        np.median(raw[geom["keep"]], axis=0),
        np.median(corrected[geom["keep"]], axis=0),
        stars["xy"][selected],
        stars["tmag"][selected],
        bright_stars=int(selected.sum()) if empirical_stars else 0,
    )
    del corrected
    design = np.column_stack([gaia, tangent, detector_background_columns(raw.shape[1:], order=2)])
    scene = SceneProjector(raw, u, v, design, use_gpu=True)
    sigma = np.full(v.shape, np.inf)
    np.divide(1.0, np.sqrt(np.median(u[u > 0]) * v), out=sigma, where=v > 0)
    quality = weighted_persistent_steps(
        scene.residual,
        geom["time"],
        geom["keep"],
        sigma,
        u,
        window=6,
        threshold=10.0,
        maximum_span_factor=1.5,
        minimum_instant_fraction=0.5,
        minimum_instant_score=8.0,
    )
    mask = quality["mask"]
    fraction = float(np.count_nonzero(mask & (v > 0)) / np.count_nonzero(v > 0))
    if not np.any((v > 0) & ~mask):
        raise QualityRejected(fraction)
    diagnostic = difficulty(scene, mask, geom["keep"], stars["tmag"])
    del scene
    return {
        "design": design,
        "weights": (u, v),
        "stars": stars,
        "quality": quality,
        "masked_fraction": fraction,
        "difficulty": diagnostic,
    }


def fit(sector, raw_override=None, fixed_weights=None, *, empirical_stars=False, screened=None):
    if screened is None:
        screened = screen(sector, raw_override, fixed_weights, empirical_stars=empirical_stars)
    elif raw_override is not None or fixed_weights is not None:
        raise ValueError("Preflight quality belongs only to the unchanged baseline")
    geom, meta = sector.geom, sector.meta
    raw = (
        np.load(sector.directory / "raw.npy", mmap_mode="r")
        if raw_override is None
        else raw_override
    )
    design, stars = screened["design"], screened["stars"]
    u, v = screened["weights"]
    quality, fraction = screened["quality"], screened["masked_fraction"]
    mask = quality["mask"]
    columns, _ = stellar_columns(
        stars["xy"], stars["tmag"], meta, raw.shape[1:], magnitude_limit=15.0, radius=6
    )
    scene = SceneProjector(
        raw, u, np.where(mask, 0.0, v), np.column_stack([design, columns]), use_gpu=True
    )
    masked = scene.pixel_weight
    basis, fold = learn_noise_weighted_basis(scene.residual, geom["keep"], u, masked, n_modes=16)
    plan = ContinuousCatalogVariancePlan(
        catalog_features(stars["xy"], stars["flux"], masked), fold, masked
    )
    temporal = AdaptiveTemporalProjector(
        scene.residual,
        (u[:, None, None] * masked).astype(np.float32),
        basis,
        fold,
        prior_variances=plan.predict(scene.residual, u, basis),
    )
    return {
        "scene": scene,
        "temporal": temporal,
        "projector": SparseComposedScorer(ComposedTemporalScorer(scene, temporal, device="cpu")),
        "weights": (u, v),
        "masked_fraction": fraction,
        "quality": quality,
    }
