"""Independent numerical reference formulas used only by tests."""

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


def profile_stationary(data, inverse_variance, template):
    """Fit one signed source amplitude plus one constant per detector pixel.

    All arrays have shape (time, ...pixels). Zero weights mask samples. The
    returned information accounts for fitting the stationary background.
    """
    data, weight, template = [
        np.asarray(a, dtype=np.float64) for a in (data, inverse_variance, template)
    ]
    if not (data.shape == weight.shape == template.shape) or data.ndim < 2:
        raise ValueError("Expected equal time-by-pixel array shapes")
    if np.any(np.isfinite(weight) & (weight < 0)):
        raise ValueError("Negative inverse variance")
    valid = np.isfinite(data) & np.isfinite(weight) & np.isfinite(template) & (weight > 0)
    weight = np.where(valid, weight, 0.0)
    data = np.where(valid, data, 0.0)
    template = np.where(valid, template, 0.0)
    total = weight.sum(axis=0)
    mean = np.divide(
        (weight * template).sum(axis=0), total, out=np.zeros_like(total), where=total > 0
    )
    projected = template - mean
    numerator = float(np.sum(weight * projected * data))
    information = float(np.sum(weight * projected**2))
    unprofiled = float(np.sum(weight * template**2))
    identifiable = information > max(1e-30, 1e-12 * unprofiled)
    return dict(
        identifiable=identifiable,
        numerator=numerator,
        information=information,
        unprofiled_information=unprofiled,
        information_fraction=information / unprofiled if unprofiled else 0.0,
        flux=numerator / information if identifiable else None,
        sigma=information**-0.5 if identifiable else None,
        formal_snr=numerator / np.sqrt(information) if identifiable else None,
    )


def persistent_steps(
    residual,
    time,
    keep,
    pixel_sigma,
    window=6,
    threshold=10.0,
    maximum_span_factor=1.5,
    minimum_instant_fraction=0.0,
    minimum_instant_score=0.0,
):
    residual = np.asarray(residual)
    time = np.asarray(time, float)
    keep = np.asarray(keep, bool)
    sigma = np.asarray(pixel_sigma, float)
    if (
        residual.ndim != 3
        or residual.shape[0] != len(time)
        or keep.shape != time.shape
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
    ):
        raise ValueError("Invalid discontinuity settings")
    selected = np.flatnonzero(keep)
    if len(selected) < 3 * window or np.any(np.diff(time[selected]) <= 0):
        raise ValueError("Need increasing times and three windows of usable samples")
    times = time[selected]
    cadence = np.median(np.diff(times))
    positions = np.arange(window, len(selected) - 2 * window + 1)
    span = times[positions + 2 * window - 1] - times[positions - window]
    valid_time = span <= maximum_span_factor * (3 * window - 1) * cadence
    # Explicitly reject any large gap within the three windows, even if other
    # intervals are sufficiently short to pass the total-span check.
    gaps = sliding_window_view(np.diff(times), 3 * window - 1).max(axis=-1)
    valid_time &= gaps <= maximum_span_factor * cadence
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
        persistent = np.where(first * second > 0, np.minimum(abs(first), abs(second)), 0.0)
        scale = sigma.ravel()[start:stop]
        valid = np.isfinite(scale) & (scale > 0)
        if minimum_instant_fraction > 0 or minimum_instant_score > 0:
            instant = values[positions] - values[positions - 1]
            abrupt = (
                (instant * first > 0)
                & (abs(instant) >= minimum_instant_fraction * persistent)
                & (abs(instant) >= minimum_instant_score * scale[None])
            )
            persistent = np.where(abrupt, persistent, 0.0)
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
