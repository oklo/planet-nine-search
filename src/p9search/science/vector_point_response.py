"""Vectorized equivalent of the installed lkprf stamp evaluation.

Preserves its full-PRF normalization, small-value cutoff, subpixel phase,
spatial grouping and finite-stamp clipping. This is an optimization candidate;
the original renderer remains the reference and is not modified.
"""

import numpy as np

from .point_response import _prf


def evaluate_many(model, targets, origin, shape, chunk=512):
    targets = np.asarray(targets, float)
    row, column = targets.T
    r1, r2 = int(np.floor(model.PRFrow[0])), int(np.ceil(model.PRFrow[-1]))
    c1, c2 = int(np.floor(model.PRFcol[0])), int(np.ceil(model.PRFcol[-1]))
    result = np.empty((len(row), *shape), float)
    for start in range(0, len(row), chunk):
        rr = row[start : start + chunk]
        cc = column[start : start + chunk]
        n = len(rr)
        dr = np.arange(r1, r2)[None] - rr[:, None] % 1
        dc = np.arange(c1, c2)[None] - cc[:, None] % 1
        values = model.interpolate(dr[:, :, None], dc[:, None, :], grid=False)
        values[np.abs(values) < 1e-6] = 0
        values /= values.sum(axis=(1, 2))[:, None, None]
        ri = origin[0] + np.arange(shape[0])[None] - np.floor(rr[:, None]).astype(int) - r1
        ci = origin[1] + np.arange(shape[1])[None] - np.floor(cc[:, None]).astype(int) - c1
        valid = (
            (ri[:, :, None] >= 0)
            & (ri[:, :, None] < r2 - r1)
            & (ci[:, None, :] >= 0)
            & (ci[:, None, :] < c2 - c1)
        )
        result[start : start + n] = np.where(
            valid,
            values[
                np.arange(n)[:, None, None],
                np.clip(ri, 0, r2 - r1 - 1)[:, :, None],
                np.clip(ci, 0, c2 - c1 - 1)[:, None, :],
            ],
            0,
        )
    return result


def tess_pixel_model(x, y, metadata, radius=3, evaluator=None):
    evaluator = evaluator or evaluate_many
    if metadata.get("analysis_transposed", False):
        return tess_pixel_model(
            y, x, dict(metadata, analysis_transposed=False), radius, evaluator
        ).swapaxes(1, 2)
    x, y = np.asarray(x), np.asarray(y)
    dx = x + metadata["origin_column"]
    dy = y + metadata["origin_row"]
    bins = np.column_stack([np.floor(dx / 8), np.floor(dy / 8)]).astype(int)
    result = np.empty((len(x), 2 * radius + 1, 2 * radius + 1))
    shape = result.shape[1:]
    model = _prf(metadata["camera"], metadata["ccd"], metadata["sector"])
    for key in np.unique(bins, axis=0):
        selected = np.all(bins == key, axis=1)
        cx, cy = int(np.rint(np.mean(dx[selected]))), int(np.rint(np.mean(dy[selected])))
        targets = list(
            zip(cy + y[selected] - np.rint(y[selected]), cx + x[selected] - np.rint(x[selected]))
        )
        model.check_coordinates(targets=targets, shape=shape)
        model._prepare_supersamp_prf(targets=targets, shape=shape)
        result[selected] = evaluator(model, targets, (cy - radius, cx - radius), shape)
    return result
