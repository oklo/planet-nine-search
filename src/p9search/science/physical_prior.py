"""Cheap, explicit promotion cuts conditional on a local P9 reference prior.

This is an operational prior envelope, not a posterior odds calculation or a
claim of physical impossibility outside the adopted population. Sparse orbit
samples are enclosed continuously rather than treated as isolated allowed paths.
"""

import math

import numpy as np

POLICY = dict(
    version="local-brown-prior-v1",
    sky_radius_deg=5.0,
    minimum_models=64,
    range_padding_fraction=0.2,
    angular_grid_padding=2.0,
    inverse_grid_padding=1.0,
    flux_sigma_allowance=5.0,
    maximum_V_minus_T=1.5,
    calibration_allowance_mag=0.5,
    optical_radius_padding=1.25,
    zero_point=20.44,
    earth_radius_km=6378.1,
    observer_solar_distance_bound_au=2.0,
)


def envelope(parameters, radii, albedos, angular_step, inverse_step):
    values = np.asarray(parameters, float)
    radii, albedos = np.asarray(radii, float), np.asarray(albedos, float)
    if (
        values.ndim != 2
        or values.shape[1] != 3
        or len(values) < 2
        or radii.shape != (len(values),)
        or albedos.shape != radii.shape
        or not np.isfinite(values).all()
        or not np.isfinite(radii).all()
        or not np.isfinite(albedos).all()
        or np.any(radii <= 0)
        or np.any(albedos <= 0)
        or np.any(values[:, 2] <= 0)
        or not angular_step > 0
        or not inverse_step > 0
    ):
        raise ValueError("Finite local qx, qy, inverse-distance and physical samples required")
    padding = np.array(
        [POLICY["angular_grid_padding"] * angular_step] * 2
        + [POLICY["inverse_grid_padding"] * inverse_step]
    )
    padding += POLICY["range_padding_fraction"] * np.ptp(values, axis=0)
    return dict(
        lower=(values.min(axis=0) - padding).tolist(),
        upper=(values.max(axis=0) + padding).tolist(),
        optical_radius_max_earth=float(
            np.max(radii * np.sqrt(albedos)) * POLICY["optical_radius_padding"]
        ),
        raw_optical_radius_max_earth=float(np.max(radii * np.sqrt(albedos))),
        angular_step=float(angular_step),
        inverse_step=float(inverse_step),
        models=len(values),
    )


def screen(parameters, flux, sigma, context):
    p = np.asarray(parameters, float)
    if (
        p.shape != (5,)
        or not np.isfinite(p).all()
        or p[4] <= 0
        or not math.isfinite(flux)
        or not math.isfinite(sigma)
        or sigma <= 0
    ):
        raise ValueError(
            "Finite candidate parameters, amplitude and positive conditional error required"
        )
    lo, hi = np.asarray(context["lower"]), np.asarray(context["upper"])
    reasons = [
        name
        for name, value, a, b in zip(
            ["motion_qx", "motion_qy", "distance"], p[2:], lo, hi, strict=True
        )
        if not a <= value <= b
    ]
    # Give the fitted interpretation one whole distance cell toward the Sun,
    # a fainter amplitude, a redder color and a fainter photometric calibration.
    distance = 1 / (p[4] + POLICY["inverse_grid_padding"] * context["inverse_step"])
    heliocentric = max(0.0, distance - POLICY["observer_solar_distance_bound_au"])
    floor = max(0.0, flux - POLICY["flux_sigma_allowance"] * sigma)
    optical_radius = 0.0
    if floor > 0:
        faint_v = (
            POLICY["zero_point"]
            - 2.5 * math.log10(floor)
            + POLICY["maximum_V_minus_T"]
            + POLICY["calibration_allowance_mag"]
        )
        optical_radius = (
            664.5 * distance * heliocentric * 10 ** (-0.2 * faint_v) / POLICY["earth_radius_km"]
        )
    if optical_radius > context["optical_radius_max_earth"]:
        reasons.append("brightness_requires_excessive_radius_or_albedo")
    return dict(
        eligible_for_p9_followup=not reasons,
        rejected_by_adopted_prior=bool(reasons),
        reasons=reasons,
        minimum_optical_radius_earth=optical_radius,
        allowed_optical_radius_earth=context["optical_radius_max_earth"],
        conservative_distance_au=distance,
        flux_floor=floor,
        tess_magnitude=POLICY["zero_point"] - 2.5 * math.log10(flux) if flux > 0 else None,
    )
