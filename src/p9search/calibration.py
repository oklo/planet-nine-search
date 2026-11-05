"""Native injection fidelity and source-transfer checks before recovery counts."""

import numpy as np


def response_check(baseline, injected, template, region, keep, flux, before, after):
    expected = flux * np.asarray(template, float)[keep]
    difference = (
        np.asarray(injected[region], float)[keep] - np.asarray(baseline[region], float)[keep]
    )
    if not np.isfinite(difference).all() or not np.isfinite(expected).all():
        return {
            "assessed": False,
            "gain": None,
            "template_rms_error": None,
            "relative_template_error": None,
            "reason": "nonfinite_source_pixels",
        }
    rms = float(np.sqrt(np.mean((difference - expected) ** 2)))
    norm = float(np.sqrt(np.mean(expected**2)))
    relative = rms / norm if norm > 0 else None
    identifiable = before["identifiable"] and after["identifiable"]
    gain = (after["flux"] - before["flux"]) / flux if identifiable else None
    assessed = bool(
        identifiable
        and gain is not None
        and np.isfinite(gain)
        and 0.9 <= gain <= 1.1
        and rms <= 0.002
        and relative is not None
        and relative <= 0.1
    )
    return {
        "assessed": assessed,
        "gain": gain,
        "template_rms_error": rms,
        "relative_template_error": relative,
    }


def has_source(template):
    template = np.asarray(template)
    if not np.isfinite(template).all():
        raise ValueError("Nonfinite source template")
    return bool(np.any(template != 0))


def unchanged_pixels(baseline, injected):
    if (
        baseline.shape != injected.shape
        or baseline.dtype != injected.dtype
        or not np.array_equal(baseline.view(np.uint8), injected.view(np.uint8))
    ):
        raise ValueError("An empty source template changed native-injected pixels")


def response_admitted(responses):
    informative = [r for r in responses if r["has_source"]]
    return len(informative) >= 3 and all(r["assessed"] for r in responses)
