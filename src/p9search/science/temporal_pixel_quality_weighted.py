"""Persistent pixel changes scaled by the actual exposure noise weights.

Thresholds retain the single-cadence convention of the original quality test.
Each before/after contrast uses the RMS of its windows' typical single-cadence
noise; the adjacent jump uses the two actual exposures' noise. This does not
treat window medians as independent Gaussian significance measurements.
"""

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


def weighted_persistent_steps(
    residual,
    time,
    keep,
    pixel_sigma,
    time_weight,
    window=6,
    threshold=10.0,
    maximum_span_factor=1.5,
    minimum_instant_fraction=0.5,
    minimum_instant_score=8.0,
):
    residual = np.asarray(residual)
    time = np.asarray(time, float)
    keep = np.asarray(keep, bool)
    sigma = np.asarray(pixel_sigma, float)
    u = np.asarray(time_weight, float)
    if (
        residual.ndim != 3
        or residual.shape[0] != len(time)
        or keep.shape != time.shape
        or u.shape != time.shape
        or sigma.shape != residual.shape[1:]
    ):
        raise ValueError("Incompatible time, residual, or noise shapes")
    if (
        window < 3
        or int(window) != window
        or threshold <= 0
        or maximum_span_factor < 1
        or not 0 <= minimum_instant_fraction <= 1
        or minimum_instant_score < 0
        or np.any(u < 0)
        or not np.isfinite(u).all()
    ):
        raise ValueError("Invalid discontinuity or time-weight settings")
    selected = np.flatnonzero(keep & (u > 0))
    if len(selected) < 3 * window or np.any(np.diff(time[selected]) <= 0):
        raise ValueError("Need increasing times and three windows of weighted samples")
    times = time[selected]
    cadence = np.median(np.diff(times))
    positions = np.arange(window, len(selected) - 2 * window + 1)
    span = times[positions + 2 * window - 1] - times[positions - window]
    valid_time = span <= maximum_span_factor * (3 * window - 1) * cadence
    gaps = sliding_window_view(np.diff(times), 3 * window - 1).max(axis=-1)
    valid_time &= gaps <= maximum_span_factor * cadence
    # pixel_sigma is expressed at median positive time weight, as in the
    # existing scene-quality interface. For equal exposure weights factors=1.
    variance_factor = np.median(u[u > 0]) / u[selected]
    median_variance = np.median(sliding_window_view(variance_factor, window), axis=-1)
    before_variance = median_variance[positions - window]
    first_scale = np.sqrt(0.5 * (before_variance + median_variance[positions]))
    second_scale = np.sqrt(0.5 * (before_variance + median_variance[positions + window]))
    instant_scale = np.sqrt(0.5 * (variance_factor[positions] + variance_factor[positions - 1]))
    pixels = int(np.prod(sigma.shape))
    score = np.zeros(pixels, float)
    amplitude = np.zeros(pixels, float)
    when = np.full(pixels, -1, int)
    flat = residual.reshape(len(time), pixels)
    for start in range(0, pixels, 256):
        stop = min(start + 256, pixels)
        values = flat[selected, start:stop]
        med = np.median(sliding_window_view(values, window, axis=0), axis=-1)
        before = med[positions - window]
        first = med[positions] - before
        second = med[positions + window] - before
        same_sign = first * second > 0
        raw_persistent = np.minimum(abs(first), abs(second))
        normalized = np.minimum(
            abs(first) / first_scale[:, None], abs(second) / second_scale[:, None]
        )
        scale = sigma.ravel()[start:stop]
        valid = np.isfinite(scale) & (scale > 0)
        instant = values[positions] - values[positions - 1]
        abrupt = (
            (instant * first > 0)
            & (abs(instant) >= minimum_instant_fraction * raw_persistent)
            & (abs(instant) >= minimum_instant_score * instant_scale[:, None] * scale[None])
        )
        persistent = np.where(same_sign & abrupt, normalized, 0.0)
        statistic = np.zeros_like(persistent)
        np.divide(persistent, scale[None], out=statistic, where=valid[None])
        statistic[~valid_time] = 0.0
        statistic = np.nan_to_num(statistic, nan=0.0, posinf=0.0, neginf=0.0)
        best = np.argmax(statistic, axis=0)
        columns = np.arange(stop - start)
        score[start:stop] = statistic[best, columns]
        amplitude[start:stop] = 0.5 * (first[best, columns] + second[best, columns])
        when[start:stop] = selected[positions[best]]
    return dict(
        mask=(score >= threshold).reshape(sigma.shape),
        maximum_step_score=score.reshape(sigma.shape),
        step_amplitude=amplitude.reshape(sigma.shape),
        time_index=when.reshape(sigma.shape),
    )
