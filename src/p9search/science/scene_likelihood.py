"""Joint spatial stellar-scene and stationary-sky nuisance likelihood.

Separable weights make the spatial and temporal projections commute. A shared
spatial basis describes star-image changes; it does not give every pixel an
arbitrary time series. Source templates pass through both nuisance projections.
The noise model remains conditional and requires empirical tail calibration.
"""

import numpy as np
from scipy.ndimage import gaussian_filter


def separable_weights(weight, keep):
    weight = np.asarray(weight)
    keep = np.asarray(keep, bool)
    with np.errstate(divide="ignore", invalid="ignore"):
        log = np.where(weight[keep] > 0, np.log(weight[keep]), np.nan)
    valid = np.isfinite(log).any(axis=0)
    pixel = np.zeros(weight.shape[1:], float)
    pixel[valid] = np.exp(np.nanmedian(log[:, valid], axis=0))
    time = np.zeros(len(weight), float)
    time[keep] = np.exp(np.nanmedian(log[:, valid] - np.log(pixel[valid]), axis=1))
    time = np.nan_to_num(time)
    return time, pixel


def scene_tangents(reference, residual_reference, star_xy=None, star_tmag=None, bright_stars=0):
    """Fixed star-guided image templates for brightness, shift and PSF changes."""
    h, w = reference.shape
    yy, xx = np.indices((h, w))
    spatial = [np.ones((h, w)), (xx - w / 2) / w, (yy - h / 2) / h]
    columns = []
    for image in [reference, residual_reference]:
        high = image - gaussian_filter(image, 8)
        features = [
            high,
            gaussian_filter(high, 0.6, order=(0, 1)),
            gaussian_filter(high, 0.6, order=(1, 0)),
            gaussian_filter(high, 0.6, order=(0, 2)),
            gaussian_filter(high, 0.6, order=(1, 1)),
            gaussian_filter(high, 0.6, order=(2, 0)),
        ]
        columns.extend((f * p).ravel() for f in features for p in spatial)
    chosen = []
    if bright_stars:
        for index in np.argsort(star_tmag):
            x, y = star_xy[index]
            if min(x, y) < 10 or x > w - 11 or y > h - 11:
                continue
            if any(np.hypot(x - xx, y - yy) < 6 for xx, yy in chosen):
                continue
            ix, iy = int(round(x)), int(round(y))
            stamp = reference[iy - 8 : iy + 9, ix - 8 : ix + 9].copy()
            dy, dx = np.mgrid[-8:9, -8:9]
            r = np.hypot(dx, dy)
            stamp -= np.median(stamp[r >= 7])
            stamp *= np.clip(8 - r, 0, 1)
            column = np.zeros((h, w))
            column[iy - 8 : iy + 9, ix - 8 : ix + 9] = stamp
            columns.append(column.ravel())
            chosen.append((x, y))
            if len(chosen) >= bright_stars:
                break
    return np.asarray(columns, dtype=np.float32).T, dict(
        tangent_columns=36, bright_star_templates=len(chosen)
    )


class SceneProjector:
    """Profile static sky per pixel and stellar-scene coefficients per frame."""

    def __init__(self, data, time_weight, pixel_weight, design, use_gpu=False, rcond=1e-6):
        data = np.asarray(data)
        self.time_weight = np.array(time_weight, dtype=float, copy=True)
        self.pixel_weight = np.asarray(pixel_weight, float)
        self.shape = data.shape
        if (
            data.ndim != 3
            or self.time_weight.shape != (len(data),)
            or self.pixel_weight.shape != data.shape[1:]
        ):
            raise ValueError("Incompatible cube and separable weight shapes")
        if np.any(self.time_weight < 0) or np.any(self.pixel_weight < 0):
            raise ValueError("Negative weight")
        if self.time_weight.sum() <= 0:
            raise ValueError("No exposure weight")
        valid = np.isfinite(data[self.time_weight > 0]).all(axis=0)
        self.pixel_weight = np.where(valid, self.pixel_weight, 0.0)
        a = np.nan_to_num(np.asarray(design, float))
        v = self.pixel_weight.ravel()
        if a.shape[0] != v.size:
            raise ValueError("Design pixel count mismatch")
        norms = np.sqrt(np.sum(v[:, None] * a * a, axis=0))
        a = a[:, norms > 0] / norms[norms > 0]
        gram = a.T @ (v[:, None] * a)
        eigen, vectors = np.linalg.eigh(gram)
        selected = eigen > max(eigen.max() * rcond, 1e-15)
        self.basis = a @ (vectors[:, selected] / np.sqrt(eigen[selected]))
        self.rank = int(selected.sum())
        matrix = np.nan_to_num(data).reshape(len(data), -1).astype(float)
        mean = self.time_weight @ matrix / self.time_weight.sum()
        matrix -= mean
        # Orthogonal spatial basis avoids amplification by ill-conditioned
        # catalog columns and permits direct sparse-template information fits.
        if use_gpu:
            import torch

            from ..runtime import device

            b = torch.tensor(self.basis.T, dtype=torch.float32, device=device())
            vb = torch.tensor(v[:, None] * self.basis, dtype=torch.float32, device=device())
            residual = np.empty_like(matrix, dtype=np.float32)
            with torch.no_grad():
                for start in range(0, len(matrix), 64):
                    y = torch.tensor(
                        matrix[start : start + 64], dtype=torch.float32, device=device()
                    )
                    residual[start : start + 64] = (y - (y @ vb) @ b).cpu().numpy()
        else:
            residual = matrix - (matrix @ (v[:, None] * self.basis)) @ self.basis.T
        self.residual = residual.reshape(data.shape)

    def score(self, template, region=None):
        if region is None:
            region = (slice(None), slice(None), slice(None))
        if region[0] != slice(None):
            raise ValueError("Full time axis required")
        p = np.asarray(template, float)
        u = self.time_weight
        v = self.pixel_weight[region[1:]].ravel()
        if p.shape != self.residual[region].shape or not np.isfinite(p).all():
            raise ValueError("Invalid source template")
        p = p.reshape(len(p), -1)
        centered = p - (u @ p / u.sum())[None]
        b = self.basis.reshape(*self.shape[1:], self.rank)[region[1:]].reshape(-1, self.rank)
        coefficients = (centered * v) @ b
        raw = float(np.sum(u[:, None] * v * p * p))
        after_static = float(np.sum(u[:, None] * v * centered * centered))
        removed = float(np.sum(u[:, None] * coefficients * coefficients))
        information = after_static - removed
        if information < -1e-8 * max(raw, 1e-30):
            raise ArithmeticError("Negative projected information")
        information = max(information, 0.0)
        numerator = float(np.sum(u[:, None] * v * p * self.residual[region].reshape(len(p), -1)))
        good = information > max(1e-30, raw * 1e-10)
        return dict(
            identifiable=good,
            numerator=numerator,
            information=information,
            information_fraction=information / raw if raw else 0.0,
            static_information_fraction=after_static / raw if raw else 0.0,
            flux=numerator / information if good else None,
            sigma=information**-0.5 if good else None,
            formal_snr=numerator / np.sqrt(information) if good else None,
        )
