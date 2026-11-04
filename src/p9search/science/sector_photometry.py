"""Combine frozen anchor photometry with every identifiable partial sector."""

import numpy as np


def coverage(positions, keep, size=140, radius=6):
    xy = np.asarray(positions, float)
    good = np.asarray(keep, bool)
    if xy.shape != (len(good), 2) or not np.isfinite(xy).all() or not good.any():
        raise ValueError("Finite native positions and good exposures required")
    centers = np.rint(xy).astype(int)
    overlap = (
        (centers[:, 0] + radius >= 0)
        & (centers[:, 0] - radius < size)
        & (centers[:, 1] + radius >= 0)
        & (centers[:, 1] - radius < size)
    )
    contained = ((xy >= radius + 1) & (xy <= size - radius - 2)).all(axis=1)
    return dict(
        coverage=float(contained[good].mean()), stamp_overlap_fraction=float(overlap[good].mean())
    )


def combine(rows, sign=1.0, minimum_anchors=3):
    """Use independent-sector scalar likelihoods; never count a sector twice.

    Anchors retain the previous >=95% center-coverage admission rule. Partial
    measurements contribute their full rendered PRF, without dividing flux or
    information by a footprint fraction. Sign changes only the null statistic.
    """
    if sign not in (-1.0, 1.0) or minimum_anchors < 1:
        raise ValueError("Invalid sign or anchor requirement")
    if len({r["sector"] for r in rows}) != len(rows):
        raise ValueError("Duplicate sector measurement")
    if sum(bool(r["anchor"]) for r in rows) < minimum_anchors:
        return None
    f = np.asarray([r["flux"] for r in rows], float)
    sigma = np.asarray([r["sigma"] for r in rows], float)
    if not np.isfinite(f).all() or not np.isfinite(sigma).all() or np.any(sigma <= 0):
        raise ValueError("Nonfinite or nonpositive scalar likelihood")
    d = sigma**-2
    amplitude = float(np.sum(f * d) / d.sum())
    error = float(d.sum() ** -0.5)
    chi = float(np.sum(d * (f - amplitude) ** 2))
    z = float(sign * amplitude / error)
    return dict(
        flux=amplitude,
        sigma=error,
        formal_snr=z,
        sector_consistency_chi2=chi,
        coherent_score=z - np.sqrt(chi),
        sectors=len(rows),
        anchors=sum(bool(r["anchor"]) for r in rows),
        partial_sectors=sum(not bool(r["anchor"]) for r in rows),
        positive_sectors=int(np.sum(sign * f > 0)),
        minimum_sector_score=float(np.min(sign * f / sigma)),
    )
