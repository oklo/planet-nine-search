"""Prospective orbit-catalog split and detector-axis costs for field placement."""

import hashlib

import numpy as np


def evaluation_sample(index):
    return bool(hashlib.sha256(f"p9 split v1/{int(index)}".encode()).digest()[0] % 2)


def band_fraction(origins, absolute_xy, size=140, width=3):
    """Fraction of each square within width pixels of any catalog row or column."""
    origins = np.asarray(origins, int)
    stars = np.asarray(absolute_xy, float).reshape(-1, 2)
    clear = np.ones(len(origins))
    for dim, high in enumerate((2092, 2048)):
        occupied = np.zeros(high + 1, dtype=bool)
        for center in stars[:, dim]:
            lo, hi = max(0, int(np.ceil(center - width))), min(high, int(np.floor(center + width)))
            if hi >= lo:
                occupied[lo : hi + 1] = True
        prefix = np.r_[0, np.cumsum(occupied)]
        start = origins[:, dim]
        n = prefix[(start + size).clip(0, high + 1)] - prefix[start.clip(0, high + 1)]
        clear *= 1 - n / size
    return 1 - clear
