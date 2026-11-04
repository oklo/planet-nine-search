import numpy as np
from scipy.fft import irfft2, next_fast_len, rfft2
from scipy.ndimage import shift


def temporal_blocks(time, time_weight, bases, block_days=0.25):
    time = np.asarray(time, float)
    u = np.asarray(time_weight, float)
    bases = np.asarray(bases, float)
    if time.shape != u.shape or bases.shape[:2] != (4, len(time)) or block_days <= 0:
        raise ValueError("Invalid time, weights, bases, or block duration")
    if not np.isfinite(time).all() or not np.isfinite(u).all() or np.any(u < 0):
        raise ValueError("Invalid time weights")
    labels = np.floor((time - time[0]) / block_days).astype(int)
    groups = [
        np.flatnonzero(labels == label)
        for label in np.unique(labels)
        if u[labels == label].sum() > 0
    ]
    if not groups:
        raise ValueError("No weighted time blocks")
    weights = np.array([u[g].sum() for g in groups])
    modes = np.stack([np.einsum("t,gtj->gj", u[g], bases[:, g]) for g in groups], axis=1)
    return groups, weights, modes


class CovarianceInformationBank:
    def __init__(
        self, projector, time_weight, pixel_weight, groups, psf, max_offset, dtype=np.float32
    ):
        self.dtype = np.dtype(dtype)
        if self.dtype not in [np.dtype("float32"), np.dtype("float64")]:
            raise ValueError("Use float32 or float64")
        self.shape = projector.shape[1:]
        u = np.asarray(time_weight, float)
        v = np.asarray(pixel_weight, float)
        self.psf = np.asarray(psf, dtype=self.dtype)
        self.max_offset = float(max_offset)
        if (
            v.shape != self.shape
            or u.shape != (projector.shape[0],)
            or self.psf.ndim != 2
            or any(s % 2 != 1 for s in self.psf.shape)
        ):
            raise ValueError("Invalid weights or odd-sized PRF")
        if (
            self.max_offset < 0
            or not np.isfinite(self.max_offset)
            or not np.isfinite(self.psf).all()
        ):
            raise ValueError("Invalid kernel bounds")
        for start in range(0, len(u), 64):
            np.testing.assert_allclose(
                projector.weight[start : start + 64],
                u[start : start + 64, None, None] * v[None],
                rtol=2e-6,
                atol=1e-30,
            )
        self.groups = [np.asarray(g, int) for g in groups]
        included = np.concatenate(self.groups)
        if len(np.unique(included)) != len(included) or set(np.flatnonzero(u > 0)) - set(included):
            raise ValueError("Time blocks overlap or omit weighted cadences")
        self.weights = np.array([u[g].sum() for g in self.groups], dtype=self.dtype)
        self.modes = np.stack(
            [np.einsum("t,gtj->gj", u[g], projector.bases[:, g]) for g in self.groups], axis=1
        ).astype(self.dtype)
        margin = int(np.ceil(self.max_offset)) + 1
        self.base = np.pad(self.psf, margin)
        self.kernel_shape = self.base.shape
        self.fft_shape = tuple(
            next_fast_len(a + b - 1) for a, b in zip(self.shape, self.kernel_shape)
        )
        k = projector.bases.shape[-1]
        images = [v.astype(self.dtype)]
        self.channels = []
        for group in range(4):
            mask = projector.fold == group
            for j in range(k):
                for mode_index in range(j, k):
                    values = (
                        v
                        * v
                        * projector.inverse_gram[:, :, j, mode_index]
                        * mask
                        * (1 if j == mode_index else 2)
                    )
                    if np.any(values):
                        images.append(values.astype(self.dtype))
                        self.channels.append((group, j, mode_index))
        self.spectrum = rfft2(np.stack(images), s=self.fft_shape, workers=1)

    def translated_templates(self, offsets):
        offsets = np.asarray(offsets, float)
        if offsets.shape != (len(self.groups), 2) or not np.isfinite(offsets).all():
            raise ValueError("Invalid block offsets")
        if np.max(np.abs(offsets)) > self.max_offset + 1e-9:
            raise ValueError("Motion exceeds kernel support")
        return np.stack(
            [
                shift(self.base, (dy, dx), order=1, mode="constant", prefilter=False)
                for dx, dy in offsets
            ]
        )

    def information(self, offsets):
        templates = self.translated_templates(offsets)
        energy = np.einsum("b,byx,byx->yx", self.weights, templates, templates, optimize=True)
        mode = np.einsum("gbk,byx->gkyx", self.modes, templates, optimize=True)
        kernels = [energy] + [
            mode[group, j] * mode[group, mode_index] for group, j, mode_index in self.channels
        ]
        spectrum = rfft2(np.stack(kernels)[:, ::-1, ::-1], s=self.fft_shape, workers=1)
        raw_frequency = self.spectrum[0] * spectrum[0]
        removed_frequency = np.einsum(
            "cyx,cyx->yx", self.spectrum[1:], spectrum[1:], optimize=False
        )
        cy, cx = np.array(self.kernel_shape) // 2
        h, w = self.shape

        def crop(frequency):
            return irfft2(frequency, s=self.fft_shape, workers=1)[cy : cy + h, cx : cx + w]

        raw = crop(raw_frequency)
        removed = crop(removed_frequency)
        information = raw - removed
        # The underlying projector stores weights in float32 even in a double
        # oracle calculation; allow its rounding plus FFT cancellation.
        tolerance = 1e-5 * max(float(np.max(np.abs(raw))), 1e-30)
        if np.min(information) < -tolerance:
            raise ArithmeticError("Negative temporal-covariance information")
        return dict(
            information=np.maximum(information, 0),
            unprofiled_information=np.maximum(raw, 0),
            removed_information=removed,
        )
