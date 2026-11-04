"""Forward a source through spatial subtraction before temporal weighting.

This restores the source transfer of the composed linear filters. The temporal
noise model is learned from the scene residual; the spatial filter also couples
pixel noise. Thus the returned sigma is a conditional proxy, not an exact noise
propagation or discovery significance. Native refits test the nonlinear part.
"""

import numpy as np
import torch


class ComposedTemporalScorer:
    def __init__(self, scene, temporal, device="cpu", dtype=None):
        if scene.shape != temporal.shape:
            raise ValueError("Scene and temporal shapes differ")
        self.shape = scene.shape
        self.device = torch.device(device)
        self.dtype = dtype or (torch.float64 if self.device.type == "cpu" else torch.float32)

        def tensor(x):
            return torch.as_tensor(np.asarray(x).copy(), device=self.device, dtype=self.dtype)

        self.time_weight = tensor(scene.time_weight)
        self.pixel_weight = tensor(scene.pixel_weight).flatten()
        self.spatial_basis = tensor(scene.basis)
        self.weight = tensor(temporal.weight).reshape(len(scene.time_weight), -1)
        self.weighted_residual = tensor(temporal.weighted_residual).reshape(self.weight.shape)
        self.bases = tensor(temporal.bases)
        self.inverse = tensor(temporal.inverse_gram).reshape(
            -1, self.bases.shape[-1], self.bases.shape[-1]
        )
        self.indices = [
            torch.as_tensor(np.flatnonzero(temporal.fold.ravel() == g), device=self.device)
            for g in range(4)
        ]

    def project_source(self, template, region=None):
        if region is None:
            region = (slice(None), slice(None), slice(None))
        if region[0] != slice(None):
            raise ValueError("Full time axis required")
        p = np.asarray(template)
        if not np.isfinite(p).all():
            raise ValueError("Nonfinite source template")
        result = torch.zeros(self.shape, device=self.device, dtype=self.dtype)
        if tuple(result[region].shape) != p.shape:
            raise ValueError("Invalid template shape")
        result[region] = torch.as_tensor(p.copy(), device=self.device, dtype=self.dtype)
        result = result.reshape(self.weight.shape)
        raw = torch.sum(self.weight * result * result)
        result = result - (self.time_weight @ result / self.time_weight.sum())[None]
        coefficients = (result * self.pixel_weight) @ self.spatial_basis
        result = result - coefficients @ self.spatial_basis.T
        return result, raw

    def score(self, template, region=None):
        with torch.no_grad():
            p, raw = self.project_source(template, region)
            numerator = torch.sum(p * self.weighted_residual)
            weighted = self.weight * p
            information = torch.sum(weighted * p)
            for g, indices in enumerate(self.indices):
                b = (self.bases[g].T @ weighted[:, indices]).T
                information -= torch.sum(
                    b * torch.bmm(self.inverse[indices], b[:, :, None])[:, :, 0]
                )
            n = float(numerator.cpu())
            d = float(information.cpu())
            raw = float(raw.cpu())
        tolerance = 2e-5 if self.dtype == torch.float32 else 1e-8
        if d < -tolerance * max(raw, 1e-30):
            raise ArithmeticError("Negative composed information")
        d = max(d, 0.0)
        good = d > max(1e-30, raw * 1e-8)
        return dict(
            numerator=n,
            information=d,
            unprofiled_information=raw,
            information_fraction=d / raw if raw else 0.0,
            identifiable=good,
            flux=n / d if good else None,
            sigma=d**-0.5 if good else None,
            formal_snr=n / np.sqrt(d) if good else None,
        )
