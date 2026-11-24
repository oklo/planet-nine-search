"""Measure detector-axis masking with the same weights for sources and injections."""

import numpy as np

from p9search.foreground import QualityRejected


def masked_screen(screen, axes, width):
    def wrapped(sector, *args, **kwargs):
        result = screen(sector, *args, **kwargs)
        stars = axes[str(sector.meta["sector"])]
        mask = result["quality"]["mask"].copy()
        for _, x, y, _ in stars:
            mask |= (abs(np.arange(mask.shape[0]) - y) <= width)[:, None]
            mask |= abs(np.arange(mask.shape[1]) - x) <= width
        valid = result["weights"][1] > 0
        if not (valid & ~mask).any():
            raise QualityRejected(1.0)
        result["quality"] = dict(result["quality"], mask=mask)
        result["masked_fraction"] = float((valid & mask).sum() / valid.sum())
        return result

    return wrapped
