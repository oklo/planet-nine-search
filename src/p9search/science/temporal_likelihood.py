import numpy as np


class TemporalProjector:
    """Precompute weighted nuisance fits and score arbitrary source templates."""

    def __init__(self, data, inverse_variance, bases, fold, n_modes=None):
        data = np.asarray(data)
        weight = np.asarray(inverse_variance)
        if data.shape != weight.shape or data.ndim != 3 or fold.shape != data.shape[1:]:
            raise ValueError("Incompatible data, weights, and fold shapes")
        if np.any(np.isfinite(weight) & (weight < 0)):
            raise ValueError("Negative inverse variance")
        self.shape = data.shape
        self.fold = np.asarray(fold)
        self.bases = np.asarray(bases, dtype=np.float64)
        if n_modes is not None:
            self.bases = self.bases[:, :, : n_modes + 1]
        if self.bases.shape[:2] != (4, data.shape[0]) or np.any(~np.isfinite(self.bases)):
            raise ValueError("Expected four finite time-by-coefficient bases")
        valid = np.isfinite(data) & np.isfinite(weight) & (weight > 0)
        self.weight = np.where(valid, weight, 0).astype(np.float32)
        values = np.where(valid, data, 0).reshape(data.shape[0], -1)
        wflat = self.weight.reshape(data.shape[0], -1)
        residual = np.zeros_like(values, dtype=np.float32)
        k = self.bases.shape[2]
        inverse = np.zeros((values.shape[1], k, k), np.float64)
        for group in range(4):
            indices = np.flatnonzero(self.fold.ravel() == group)
            u = self.bases[group]
            pair = (u[:, :, None] * u[:, None, :]).reshape(len(u), -1)
            for start in range(0, len(indices), 512):
                pix = indices[start : start + 512]
                ww = wflat[:, pix].astype(float)
                yy = values[:, pix].astype(float)
                gram = (pair.T @ ww).T.reshape(len(pix), k, k)
                inv = np.linalg.pinv(gram, rcond=1e-10, hermitian=True)
                rhs = u.T @ (ww * yy)
                coefficients = np.einsum("pij,jp->ip", inv, rhs)
                residual[:, pix] = ww * (yy - u @ coefficients)
                inverse[pix] = inv
        self.weighted_residual = residual.reshape(data.shape)
        self.inverse_gram = inverse.reshape(*data.shape[1:], k, k)

    def score(self, template, region=None):
        if region is None:
            region = (slice(None), slice(None), slice(None))
        if region[0] != slice(None):
            raise ValueError("Score the full time axis; mask time through weights")
        p = np.asarray(template, float)
        w = self.weight[region].astype(float)
        if p.shape != w.shape or np.any(~np.isfinite(p)):
            raise ValueError("Invalid source template")
        numerator = float(np.sum(p * self.weighted_residual[region], dtype=np.float64))
        raw_information = float(np.sum(w * p * p))
        labels = self.fold[region[1:]].ravel()
        inv = self.inverse_gram[region[1:]].reshape(-1, self.bases.shape[2], self.bases.shape[2])
        wp = (w * p).reshape(len(p), -1)
        removed = 0.0
        for group in range(4):
            indices = np.flatnonzero(labels == group)
            b = self.bases[group].T @ wp[:, indices]
            removed += float(np.einsum("ip,pij,jp->", b, inv[indices], b, optimize=True))
        information = raw_information - removed
        if information < -1e-8 * max(raw_information, 1e-30):
            raise ArithmeticError("Negative projected information")
        information = max(0.0, information)
        identifiable = information > max(1e-30, 1e-10 * raw_information)
        return dict(
            numerator=numerator,
            information=information,
            unprofiled_information=raw_information,
            information_fraction=information / raw_information if raw_information else 0.0,
            identifiable=identifiable,
            flux=numerator / information if identifiable else None,
            sigma=information**-0.5 if identifiable else None,
            formal_snr=numerator / np.sqrt(information) if identifiable else None,
        )
