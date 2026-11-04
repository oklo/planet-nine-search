"""Predict temporal variances from separated pixels with similar Gaia context.

The target pixel and its neighborhood cannot set its own variance. Predictors
use catalog-only brightness maps and frozen diagonal weights. All donor mode
powers are expressed in the target fold's temporal basis. Spatial cross-fitting
reduces source leakage but does not replace native injection/null calibration.
"""

import numpy as np
from scipy.ndimage import gaussian_filter
from scipy.spatial import cKDTree


def catalog_features(xy, flux, pixel_weight):
    v = np.asarray(pixel_weight, float)
    h, w = v.shape
    pad = 16
    image = np.zeros((h + 2 * pad, w + 2 * pad), float)
    xy = np.asarray(xy, float) + pad
    flux = np.asarray(flux, float)
    good = np.isfinite(xy).all(axis=1) & np.isfinite(flux) & (flux > 0)
    xy = xy[good]
    flux = flux[good]
    base = np.floor(xy).astype(int)
    fraction = xy - base
    for dx, dy in [(0, 0), (1, 0), (0, 1), (1, 1)]:
        ix = base[:, 0] + dx
        iy = base[:, 1] + dy
        valid = (ix >= 0) & (ix < image.shape[1]) & (iy >= 0) & (iy < image.shape[0])
        weight = (fraction[:, 0] if dx else 1 - fraction[:, 0]) * (
            fraction[:, 1] if dy else 1 - fraction[:, 1]
        )
        np.add.at(image, (iy[valid], ix[valid]), (flux * weight)[valid])
    # Gaussian catalog maps are context features, not a replacement TESS PRF.
    maps = [
        gaussian_filter(image, sigma)[pad : pad + h, pad : pad + w] for sigma in [0.7, 1.5, 3.0]
    ]
    gy, gx = np.gradient(maps[0])
    gradient = np.hypot(gx, gy)
    noise = np.full(v.shape, np.max(1 / v[v > 0]) if np.any(v > 0) else 1.0)
    np.divide(1.0, v, out=noise, where=v > 0)
    return np.stack([np.log1p(m) for m in [*maps, gradient, noise]], axis=-1)


class CatalogVariancePlan:
    def __init__(
        self,
        features,
        fold,
        pixel_weight,
        neighbors=64,
        exclusion_radius=12.0,
        quantile=0.75,
        activation_sigma=5.0,
    ):
        self.fold = np.asarray(fold)
        self.shape = self.fold.shape
        self.v = np.asarray(pixel_weight, float)
        x = np.asarray(features, float).reshape(-1, features.shape[-1])
        if (
            self.v.shape != self.shape
            or features.shape[:2] != self.shape
            or not np.isfinite(x).all()
        ):
            raise ValueError("Invalid features or weights")
        if neighbors < 4 or exclusion_radius < 0 or not 0 < quantile < 1 or activation_sigma <= 0:
            raise ValueError("Invalid variance-prediction settings")
        scale = np.quantile(x, 0.75, axis=0) - np.quantile(x, 0.25, axis=0)
        x = (x - np.median(x, axis=0)) / np.maximum(scale, 0.1)
        yy, xx = np.indices(self.shape)
        coordinates = np.c_[xx.ravel(), yy.ravel()]
        self.donors = np.full((len(x), neighbors), -1, np.int32)
        valid = np.isfinite(self.v.ravel()) & (self.v.ravel() > 0)
        for group in range(4):
            targets = np.flatnonzero(self.fold.ravel() == group)
            pool = np.flatnonzero((self.fold.ravel() != group) & valid)
            if len(pool) < neighbors:
                raise ValueError("Insufficient independent donor pixels")
            tree = cKDTree(x[pool])
            query_count = min(len(pool), max(256, neighbors * 4))
            for start in range(0, len(targets), 256):
                selected = targets[start : start + 256]
                _, indices = tree.query(x[selected], k=query_count, workers=1)
                candidate = pool[indices]
                separate = (
                    np.linalg.norm(coordinates[candidate] - coordinates[selected, None], axis=2)
                    > exclusion_radius
                )
                for row, pixel in enumerate(selected):
                    chosen = candidate[row, separate[row]]
                    if len(chosen) < neighbors:
                        _, all_indices = tree.query(x[pixel], k=len(pool), workers=1)
                        all_candidates = pool[all_indices]
                        separated = (
                            np.linalg.norm(coordinates[all_candidates] - coordinates[pixel], axis=1)
                            > exclusion_radius
                        )
                        chosen = all_candidates[separated]
                    if len(chosen) < neighbors:
                        raise ValueError("Insufficient spatially separated donors")
                    self.donors[pixel] = chosen[:neighbors]
        self.quantile = quantile
        self.activation_sigma = activation_sigma
        self.exclusion_radius = exclusion_radius

    def predict(self, residual, time_weight, bases):
        residual = np.asarray(residual)
        u = np.asarray(time_weight, float)
        bases = np.asarray(bases, float)
        if (
            residual.shape[1:] != self.shape
            or residual.shape[0] != len(u)
            or bases.shape[:2] != (4, len(u))
        ):
            raise ValueError("Incompatible residual, time weights, or bases")
        k = bases.shape[2] - 1
        answer = np.zeros((*self.shape, k), float)
        flat = answer.reshape(-1, k)
        values = np.nan_to_num(residual).reshape(len(u), -1).astype(float)
        v = self.v.ravel()
        for group in range(4):
            basis = bases[group]
            gram = basis.T @ (u[:, None] * basis)
            inverse = np.linalg.pinv(gram, rcond=1e-10, hermitian=True)
            coefficient = (inverse @ (basis.T * u)) @ values
            diagonal = np.maximum(np.diag(inverse)[1:], 1e-30)
            ratio = (coefficient[1:] ** 2 * v[None] / diagonal[:, None]).T
            targets = np.flatnonzero((self.fold.ravel() == group) & (v > 0))
            for start in range(0, len(targets), 256):
                pixels = targets[start : start + 256]
                predicted = np.quantile(ratio[self.donors[pixels]], self.quantile, axis=1)
                excess = np.where(
                    predicted > self.activation_sigma**2, np.maximum(predicted - 1, 0), 0.0
                )
                flat[pixels] = excess * diagonal[None] / v[pixels, None]
        return answer
