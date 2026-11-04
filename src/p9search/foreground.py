"""Fit the same Gaia scene and temporal covariance to data and source models."""

import numpy as np

from .runtime import device
from .science.adaptive_covariance import AdaptiveTemporalProjector
from .science.catalog_covariance import catalog_features
from .science.catalog_star_scene import stellar_columns
from .science.composed_scoring import ComposedTemporalScorer
from .science.continuous_catalog_covariance import ContinuousCatalogVariancePlan
from .science.detector_background import detector_background_columns
from .science.noise_weighted_temporal_basis import learn_noise_weighted_basis
from .science.preprocessing import gaia_design, linear_scene_subtract, preprocess
from .science.scene_likelihood import SceneProjector, scene_tangents, separable_weights
from .science.temporal_pixel_quality_weighted import weighted_persistent_steps


class QualityRejected(RuntimeError):
    def __init__(self, fraction):
        super().__init__(f"Abrupt-pixel mask covers {fraction:.2%}; limit is 2%")
        self.fraction = fraction


def fit(sector, raw_override=None, fixed_weights=None):
    geom, meta = sector.geom, sector.meta
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
    corrected, _ = linear_scene_subtract(raw, geom["keep"], gaia)
    tangent, _ = scene_tangents(
        np.median(raw[geom["keep"]], axis=0),
        np.median(corrected[geom["keep"]], axis=0),
        stars["xy"],
        stars["tmag"],
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
    fraction = float(mask.sum() / np.sum(v > 0))
    if fraction > 0.02:
        raise QualityRejected(fraction)
    del scene
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
        "projector": ComposedTemporalScorer(scene, temporal, device=device()),
        "weights": (u, v),
        "masked_fraction": fraction,
        "quality": quality,
    }
