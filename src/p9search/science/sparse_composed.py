"""Localized CPU float64 contraction of the composed conditional likelihood."""

import numpy as np
import torch


class SparseComposedScorer:
    def __init__(self, original):
        if original.device.type != "cpu" or original.dtype != torch.float64:
            raise ValueError("Sparse scoring requires original CPU float64 coefficients")
        gram = original.spatial_basis.T @ (original.pixel_weight[:, None] * original.spatial_basis)
        if not torch.allclose(
            gram, torch.eye(len(gram), dtype=torch.float64), rtol=1e-7, atol=1e-7
        ):
            raise ValueError("Sparse scoring requires a weighted orthonormal spatial basis")
        self.original = original
        self.device, self.dtype = original.device, original.dtype
        self.shape = original.shape
        self.u, self.v = original.time_weight, original.pixel_weight
        self.basis = original.spatial_basis
        self.groups = [indices.cpu().numpy() for indices in original.indices]
        self.fold = np.empty(int(np.prod(self.shape[1:])), dtype=int)
        for group, indices in enumerate(self.groups):
            self.fold[indices] = group
        if not torch.allclose(original.weight, self.u[:, None] * self.v, rtol=2e-6, atol=0):
            raise ValueError("Sparse contraction requires separable weights")
        self.group_basis = [self.basis[indices].T.contiguous() for indices in original.indices]
        self.group_weight = [self.v[indices] for indices in original.indices]
        self.group_inverse = [original.inverse[indices] for indices in original.indices]
        # Numerator is <template, P_static^T P_spatial^T weighted_residual>.
        # The two projections commute because each acts along a different axis.
        self.adjoint = torch.empty_like(original.weighted_residual)
        correction = original.weighted_residual.sum(dim=0) / self.u.sum()
        weighted_basis = self.basis.T * self.v
        with torch.no_grad():
            for start in range(0, self.shape[0], 64):
                block = original.weighted_residual[start : start + 64]
                block = block - self.u[start : start + 64, None] * correction
                self.adjoint[start : start + 64] = block - (block @ self.basis) @ weighted_basis
        self.fallbacks = 0

    def tensor(self, value, dtype=None):
        return torch.as_tensor(
            np.asarray(value).copy(), device=self.device, dtype=dtype or self.dtype
        )

    def score(self, template, region=None):
        if region is None:
            region = (slice(None), slice(None), slice(None))
        if region[0] != slice(None):
            raise ValueError("Full time axis required")
        value = np.asarray(template)
        pixels = np.arange(np.prod(self.shape[1:])).reshape(self.shape[1:])[region[1:]]
        if value.shape != (self.shape[0], *pixels.shape) or not np.isfinite(value).all():
            raise ValueError("Invalid finite localized template")
        pixels = pixels.ravel()
        indices = self.tensor(pixels, torch.long)
        with torch.no_grad():
            source = self.tensor(value).reshape(self.shape[0], -1)
            centered = source - (self.u @ source / self.u.sum())[None]
            local_weight, local_basis = self.v[indices], self.basis[indices]
            coefficients = (centered * local_weight) @ local_basis
            information = torch.sum(self.u[:, None] * centered.square() * local_weight)
            information -= torch.sum(self.u[:, None] * coefficients.square())
            # Project the short temporal modes first. Never expand the
            # projected source to all pixels at every time step.
            for group, group_pixels in enumerate(self.groups):
                projected = self.original.bases[group].T @ (self.u[:, None] * centered)
                low_rank = (projected * local_weight) @ local_basis
                temporal = -(low_rank @ self.group_basis[group])
                within = np.flatnonzero(self.fold[pixels] == group)
                target = np.searchsorted(group_pixels, pixels[within])
                temporal[:, self.tensor(target, torch.long)] += projected[
                    :, self.tensor(within, torch.long)
                ]
                temporal = (temporal * self.group_weight[group]).T.contiguous()
                information -= torch.sum(
                    temporal * torch.bmm(self.group_inverse[group], temporal[:, :, None])[:, :, 0]
                )
            numerator = torch.sum(source * self.adjoint[:, indices])
            raw = torch.sum(self.original.weight[:, indices] * source.square())
            n, d, raw = [float(x.cpu()) for x in [numerator, information, raw]]
        # Subtractive cancellation can be poor for nearly unidentifiable
        # templates. Keep the original stable full-source calculation there.
        if not np.isfinite([n, d, raw]).all() or d <= max(1e-30, 2e-5 * raw):
            self.fallbacks += 1
            return self.original.score(template, region)
        return dict(
            numerator=n,
            information=d,
            unprofiled_information=raw,
            information_fraction=d / raw if raw else 0.0,
            identifiable=True,
            flux=n / d,
            sigma=d**-0.5,
            formal_snr=n / np.sqrt(d),
        )
