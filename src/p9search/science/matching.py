"""Geometry-only matching on epochs where an injected source can be observed."""

import copy

import numpy as np


def trajectory_distances(proposals, parameters, radial, sectors, samples_per_sector=17):
    """Return an N by 1 distance matrix for one isolated injected source.

    Uniform deterministic samples of good binned exposures with true PRF-box
    overlap give each observed sector at most17samples. No image, candidate
    score, or proposed-path visibility influences sample selection.
    """
    proposals = np.asarray(proposals, float).reshape(-1, 5)
    parameters = np.asarray(parameters, float)
    if (
        parameters.shape != (5,)
        or not np.isfinite(parameters).all()
        or not np.isfinite(proposals).all()
        or not np.isfinite(radial)
        or not isinstance(samples_per_sector, (int, np.integer))
        or samples_per_sector < 1
    ):
        raise ValueError("Finite trajectories and positive sample count required")
    selected = []
    truth = []
    context = []
    for sector in sectors:
        positions = sector.exact_positions(parameters, kepler=True, radial_velocity=radial)
        center = np.rint(positions)
        good = np.asarray(sector.geom["keep"], bool) & ((center + 6 >= 0) & (center - 6 < 140)).all(
            axis=1
        )
        available = np.flatnonzero(good)
        if not len(available):
            continue
        indices = available[
            np.rint(
                np.linspace(0, len(available) - 1, min(samples_per_sector, len(available)))
            ).astype(int)
        ]
        sliced = copy.copy(sector)
        sliced.geom = dict(sector.geom)
        for key in ["time", "dt", "pointing", "keep"]:
            sliced.geom[key] = sector.geom[key][indices]
        selected.append(sliced)
        truth.append(positions[indices])
        context.append(
            dict(
                sector=int(sector.meta["sector"]),
                available_bins=len(available),
                sampled_indices=indices.tolist(),
                sampled_times=sector.geom["time"][indices].tolist(),
            )
        )
    if not selected:
        raise ValueError("Injected trajectory has no observed support")
    target = np.concatenate(truth)
    result = []
    for p in proposals:
        prediction = np.concatenate([s.exact_positions(p, kepler=True) for s in selected])
        result.append(float(np.sqrt(np.mean(np.sum((prediction - target) ** 2, axis=1)))))
    distances = np.asarray(result).reshape(-1, 1)
    if not np.isfinite(distances).all():
        raise ValueError("Nonfinite trajectory distance")
    return distances, context
