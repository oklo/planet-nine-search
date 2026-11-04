"""Pixel-dependent low-rank temporal covariance for residual stellar variability.

A constant is profiled freely at every pixel. Other cross-fitted temporal modes
receive finite variances only where their coefficients strongly exceed the
diagonal-noise expectation. The conditional GLS algebra is exact for this
model; learned variances and upstream scene subtraction need native-injection
and empirical null calibration. This is not a discovery significance model.
"""

import numpy as np

from .temporal_likelihood import TemporalProjector


class AdaptiveTemporalProjector(TemporalProjector):
    def __init__(
        self, data, inverse_variance, bases, fold, activation_sigma=5.0, prior_variances=None
    ):
        data = np.asarray(data)
        weight = np.asarray(inverse_variance)
        self.shape = data.shape
        self.fold = np.asarray(fold)
        self.bases = np.asarray(bases, float)
        if data.shape != weight.shape or data.ndim != 3 or self.fold.shape != data.shape[1:]:
            raise ValueError("Incompatible data, weights, and fold shapes")
        if self.bases.shape[:2] != (4, len(data)) or not np.isfinite(self.bases).all():
            raise ValueError("Expected four finite time-by-coefficient bases")
        if np.any(np.isfinite(weight) & (weight < 0)) or activation_sigma <= 0:
            raise ValueError("Invalid weights or activation threshold")
        k = self.bases.shape[2]
        valid = np.isfinite(data) & np.isfinite(weight) & (weight > 0)
        self.weight = np.where(valid, weight, 0).astype(np.float32)
        values = np.where(valid, data, 0).reshape(len(data), -1)
        wflat = self.weight.reshape(len(data), -1)
        supplied = None
        if prior_variances is not None:
            supplied = np.asarray(prior_variances, float)
            if (
                supplied.shape != (*self.shape[1:], k - 1)
                or np.any(supplied < 0)
                or not np.isfinite(supplied).all()
            ):
                raise ValueError("Prior variances must be nonnegative pixel-by-mode values")
            supplied = supplied.reshape(int(np.prod(self.shape[1:])), k - 1)
        residual = np.zeros_like(values, dtype=np.float32)
        inverse = np.zeros((values.shape[1], k, k), float)
        variances = np.zeros((values.shape[1], k - 1), float)
        activated = np.zeros(values.shape[1], int)
        for group in range(4):
            indices = np.flatnonzero(self.fold.ravel() == group)
            u = self.bases[group]
            pair = (u[:, :, None] * u[:, None, :]).reshape(len(u), -1)
            for start in range(0, len(indices), 512):
                pix = indices[start : start + 512]
                ww = wflat[:, pix].astype(float)
                yy = values[:, pix].astype(float)
                gram = (pair.T @ ww).T.reshape(len(pix), k, k)
                rhs = u.T @ (ww * yy)
                if supplied is None:
                    unregularized = np.linalg.pinv(gram, rcond=1e-10, hermitian=True)
                    coefficient = np.einsum("pij,jp->pi", unregularized, rhs)
                    noise = np.maximum(np.diagonal(unregularized, axis1=1, axis2=2)[:, 1:], 0)
                    power = coefficient[:, 1:] ** 2
                    lam = np.where(
                        (power > activation_sigma**2 * noise) & (noise > 0),
                        np.maximum(power - noise, 0),
                        0.0,
                    )
                else:
                    lam = supplied[pix]
                active = np.column_stack([gram[:, 0, 0] > 0, lam > 0])
                mask = active[:, :, None] * active[:, None, :]
                regularized = gram * mask
                precision = np.zeros((len(pix), k))
                np.divide(1.0, lam, out=precision[:, 1:], where=lam > 0)
                precision[~active] = 1.0
                diagonal = np.arange(k)
                regularized[:, diagonal, diagonal] += precision
                scale = np.sqrt(np.maximum(np.diagonal(regularized, axis1=1, axis2=2), 1e-300))
                normalized = regularized / (scale[:, :, None] * scale[:, None, :])
                inv = np.linalg.pinv(normalized, rcond=1e-10, hermitian=True) / (
                    scale[:, :, None] * scale[:, None, :]
                )
                inv *= mask
                coefficients = np.einsum("pij,jp->ip", inv, rhs)
                residual[:, pix] = ww * (yy - u @ coefficients)
                inverse[pix] = inv
                variances[pix] = lam
                activated[pix] = np.sum(lam > 0, axis=1)
        self.weighted_residual = residual.reshape(data.shape)
        self.inverse_gram = inverse.reshape(*data.shape[1:], k, k)
        self.mode_variances = variances.reshape(*data.shape[1:], k - 1)
        self.active_counts = activated.reshape(data.shape[1:])
        self.activation_sigma = activation_sigma
