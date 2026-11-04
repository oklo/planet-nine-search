"""Certified compact diversity coordinates, with conservative fallback.

A certificate applies
only to the supplied finite motion bank and the stated arithmetic model. Device
and whole-search replay are separate requirements before deployment.
"""

import hashlib

import numpy as np


def certified_coordinates(
    motions, embeddings, operator, origin, step, *, minimum_rms=1.5, maximum_half_width=32
):
    """Return FP32 three-coordinate metric only when every decision is bounded.

    A motion is represented by a nearby integer lattice point. Whole-bank
    residual bounds absorb rounding and any deviations from that lattice.
    Enumerate every integer difference that could lie near the suppression
    radius; singular-value lower bounds exclude all remaining differences.
    Uncertain cases return the original FP32 embeddings, without modifying them.
    """
    motions = np.asarray(motions, dtype=np.float64)
    embeddings = np.asarray(embeddings, dtype=np.float32)
    operator = np.asarray(operator, dtype=np.float64)
    origin, step = np.asarray(origin, dtype=float), np.asarray(step, dtype=float)
    if (
        motions.ndim != 2
        or motions.shape[1] != 3
        or len(motions) == 0
        or embeddings.ndim != 2
        or embeddings.shape[0] != len(motions)
        or embeddings.shape[1] < 3
        or operator.shape != (3, embeddings.shape[1])
        or origin.shape != (3,)
        or step.shape != (3,)
    ):
        raise ValueError("Incompatible motion metric arrays")
    if (
        not all(np.isfinite(a).all() for a in [motions, embeddings, operator, origin, step])
        or np.any(step <= 0)
        or not np.isfinite(minimum_rms)
        or minimum_rms <= 0
        or not isinstance(maximum_half_width, int)
        or not 1 <= maximum_half_width <= 64
    ):
        raise ValueError("Invalid motion metric values or enumeration bound")
    threshold = float(np.float32(minimum_rms**2))
    if not np.isfinite(threshold) or threshold <= 0:
        raise ValueError("Invalid FP32 distance threshold")
    report = dict(
        certified=False,
        selected_dimensions=embeddings.shape[1],
        motions=len(motions),
        ordered_pairs=len(motions) ** 2,
        threshold_squared_fp32=threshold,
        arithmetic_model="Finite FP32 subtract/square/reduce; 16x gamma allowance. Device replay required.",
        original_embedding_sha256=hashlib.sha256(embeddings.tobytes()).hexdigest(),
    )

    def fallback(reason):
        report["fallback_reason"] = reason
        return embeddings, report

    _, triangular = np.linalg.qr(operator.T, mode="reduced")
    compact_operator = triangular.T
    compact = (motions @ compact_operator).astype(np.float32)
    if not np.isfinite(compact).all():
        return fallback("compact coordinates overflowed")
    floating_indices = np.rint((motions - origin) / step)
    if not np.isfinite(floating_indices).all() or np.max(np.abs(floating_indices)) > 2**40:
        return fallback("lattice indices exceed exact-integer working range")
    indices = floating_indices.astype(np.int64)
    affine = origin + indices * step
    lattices = dict(full=step[:, None] * operator, compact=step[:, None] * compact_operator)
    if not all(np.isfinite(value).all() for value in lattices.values()):
        return fallback("lattice coordinates overflowed")
    singular = {name: np.linalg.svd(value, compute_uv=False) for name, value in lattices.items()}
    lower_singular = min(float(v[-1]) for v in singular.values()) * (1 - 1e-6)
    report["lattice_singular_values"] = {k: v.tolist() for k, v in singular.items()}
    if not np.isfinite(lower_singular) or lower_singular <= 0:
        return fallback("degenerate lattice metric")
    if any(value[-1] <= value[0] * 1e-8 for value in singular.values()):
        return fallback("lattice is too ill-conditioned for the singular-value allowance")

    # Bound computed affine/dot-product evaluation against exact real arithmetic
    # on the stored FP64 operands; retain an additional absolute slack.
    gamma64 = (16 * 2.0**-53) / (1 - 16 * 2.0**-53)
    absolute_slack = 1e-9 * max(1.0, threshold)
    residuals = {}
    for name, values, matrix in [
        ("full", embeddings, operator),
        ("compact", compact, compact_operator),
    ]:
        expected = affine @ matrix
        evaluation_bound = gamma64 * ((np.abs(origin) + np.abs(indices) * step) @ np.abs(matrix))
        residuals[name] = (
            float(np.linalg.norm(values.astype(float) - expected, axis=1).max())
            + float(np.linalg.norm(evaluation_bound, axis=1).max())
            + absolute_slack
        )
    report["row_error_bounds"] = residuals
    radius = max(residuals.values())
    limit = int(np.ceil((np.sqrt(threshold) + 4 * radius + 1e-3) / lower_singular))
    if limit > maximum_half_width:
        return fallback("required integer difference enumeration exceeds cap")
    axis = np.arange(-limit, limit + 1, dtype=np.int64)
    delta = np.stack(np.meshgrid(axis, axis, axis, indexing="ij"), axis=-1).reshape(-1, 3)
    report["integer_difference_cube_half_width"] = limit
    report["enumerated_integer_differences"] = len(delta)
    intervals, classes = {}, {}
    for name, dimension in [("full", embeddings.shape[1]), ("compact", 3)]:
        norm = np.linalg.norm(delta @ lattices[name], axis=1)
        # Here enumeration coefficients are small; account for the FP64 dot
        # and norm error independently of the whole-bank residual bound.
        norm_error = (
            float(np.linalg.norm(gamma64 * np.abs(delta) @ np.abs(lattices[name]), axis=1).max())
            + absolute_slack
        )
        error = 2 * residuals[name] + norm_error
        u = 2.0**-24
        gamma = 16 * ((dimension + 3) * u / (1 - (dimension + 3) * u))
        low = np.maximum(0, norm - error) ** 2 * (1 - gamma) - absolute_slack
        high = (norm + error) ** 2 * (1 + gamma) + absolute_slack
        uncertain = (low <= threshold) & (high >= threshold)
        outside_low = (
            max(0, lower_singular * (limit + 1) - error) ** 2 * (1 - gamma) - absolute_slack
        )
        intervals[name] = dict(
            ambiguous_differences=int(uncertain.sum()),
            outside_lower_squared_distance=float(outside_low),
            minimum_threshold_margin=float(
                np.min(np.where(high < threshold, threshold - high, low - threshold))
            ),
        )
        classes[name] = high < threshold
    report["intervals"] = intervals
    if any(
        v["ambiguous_differences"] or v["outside_lower_squared_distance"] <= threshold
        for v in intervals.values()
    ):
        return fallback("strict suppression boundary cannot be separated from rounding bounds")
    if not np.array_equal(classes["full"], classes["compact"]):
        return fallback("compact and original metrics imply different suppression decisions")
    report.update(
        certified=True,
        selected_dimensions=3,
        compact_embedding_sha256=hashlib.sha256(compact.tobytes()).hexdigest(),
    )
    return compact, report
