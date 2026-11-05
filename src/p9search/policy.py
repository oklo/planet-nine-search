"""Task-bound choices for the released scientific policy."""

import math

from .science.physical_prior import POLICY, screen

VERSION = "tess-gaia-hough-v2"
POPULATION_SHA256 = "050b68182ecbf7fd76f280d8cc43c0683d207499aba1488a68092b834420a422"


def default_policy():
    return {"version": VERSION, "empirical_star_sectors": [], "prior": None}


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_policy(value, sectors):
    if not isinstance(value, dict) or set(value) != {"version", "empirical_star_sectors", "prior"}:
        raise ValueError("Invalid scientific policy")
    if value["version"] != VERSION:
        raise ValueError("Unsupported scientific policy")
    empirical = value["empirical_star_sectors"]
    if (
        not isinstance(empirical, list)
        or any(type(s) is not int or s not in sectors for s in empirical)
        or len(set(empirical)) != len(empirical)
    ):
        raise ValueError("Empirical-star sectors must be a distinct subset of the task")
    prior = value["prior"]
    if prior is None:
        return
    fields = {
        "version",
        "population_sha256",
        "lower",
        "upper",
        "optical_radius_max_earth",
        "angular_step",
        "inverse_step",
        "models",
        "reference_ids",
        "sky_radius_deg",
        "epoch_jd",
        "reference_frame_center",
    }
    if not isinstance(prior, dict) or set(prior) != fields:
        raise ValueError("Invalid local prior context")
    if prior["version"] != POLICY["version"] or prior["population_sha256"] != POPULATION_SHA256:
        raise ValueError("Unsupported reference population or prior")
    for name, size in (("lower", 3), ("upper", 3), ("reference_frame_center", 2)):
        if (
            not isinstance(prior[name], list)
            or len(prior[name]) != size
            or not all(map(finite, prior[name]))
        ):
            raise ValueError("Invalid prior coordinates")
    if any(a >= b for a, b in zip(prior["lower"], prior["upper"], strict=True)):
        raise ValueError("Empty prior envelope")
    for name in (
        "optical_radius_max_earth",
        "angular_step",
        "inverse_step",
        "sky_radius_deg",
        "epoch_jd",
    ):
        if not finite(prior[name]) or prior[name] <= 0:
            raise ValueError("Invalid prior scale")
    ids = prior["reference_ids"]
    if (
        type(prior["models"]) is not int
        or not 64 <= prior["models"] <= 21740
        or not isinstance(ids, list)
        or len(ids) != prior["models"]
        or any(type(i) is not int or not 0 <= i < 100000 for i in ids)
        or len(set(ids)) != len(ids)
    ):
        raise ValueError("Invalid prior sample identities")


def check_prior_geometry(context, frame, grid):
    if context is None:
        return
    import numpy as np

    inverse = (1 / grid["distance_au"][0] - 1 / grid["distance_au"][1]) / (
        grid["inverse_distance_slices"] - 1
    )
    if (
        not np.allclose(context["reference_frame_center"], frame.center, rtol=0, atol=1e-9)
        or not math.isclose(
            context["epoch_jd"], frame.reference_time + 2457000.0, rel_tol=0, abs_tol=1e-8
        )
        or not math.isclose(
            context["angular_step"], grid["joint_angular_step_rad_per_day"], rel_tol=1e-10
        )
        or not math.isclose(context["inverse_step"], inverse, rel_tol=1e-10)
    ):
        raise ValueError("Prior context belongs to different geometry or lattice")


def screen_rows(rows, context):
    if context is not None:
        for row in rows:
            if row["native"] is not None:
                row["prior"] = screen(
                    row["parameters"], row["native"]["flux"], row["native"]["sigma"], context
                )
