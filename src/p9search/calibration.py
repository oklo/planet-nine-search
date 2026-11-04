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
