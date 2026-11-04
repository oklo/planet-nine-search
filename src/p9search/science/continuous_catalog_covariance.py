"""Continuous onset for catalog-context temporal variance regularization.

The original activation rule jumps from zero variance to24noise-variance units
at a5sigma power threshold. Here the excess starts continuously at zero:
max(predicted normalized mode power - activation_sigma**2, 0). Donors and the
spatial exclusion remain unchanged. Native refits must validate source transfer.
"""

import numpy as np

from .catalog_covariance import CatalogVariancePlan


class ContinuousCatalogVariancePlan(CatalogVariancePlan):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.activation_sigma < 1:
            raise ValueError("Continuous excess-power threshold must be at least1sigma")

    def predict(self, residual, time_weight, bases):
        # Reuse the exact original donor prediction. For power q>c>=1 its
        # variance is(q-1)*noise; subtracting(c-1)*noise gives(q-c)*noise.
        # For q<=c both the original and continuous rules return zero.
        variance = super().predict(residual, time_weight, bases)
        u = np.asarray(time_weight, float)
        bases = np.asarray(bases, float)
        for group in range(4):
            basis = bases[group]
            inverse = np.linalg.pinv(basis.T @ (u[:, None] * basis), rcond=1e-10, hermitian=True)
            diagonal = np.maximum(np.diag(inverse)[1:], 1e-30)
            selected = (self.fold == group) & (self.v > 0)
            correction = (self.activation_sigma**2 - 1) * diagonal[None] / self.v[selected, None]
            variance[selected] = np.maximum(variance[selected] - correction, 0.0)
        return variance
