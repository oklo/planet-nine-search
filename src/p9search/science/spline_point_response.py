"""Evaluate the calibrated PRF's cubic B-spline on a GPU in bounded batches.

The original renderer is the oracle. Spatial interpolation and stamp conventions
are unchanged; float32 GPU arithmetic requires native-template verification.
"""

import numpy as np
import torch

from ..runtime import device as selected_device
from .vector_point_response import tess_pixel_model as grouped_render


def basis_weights(knots, points, degree):
    knots = np.asarray(knots, float)
    points = np.clip(np.asarray(points, float), knots[degree], knots[-degree - 1])
    count = len(knots) - degree - 1
    span = np.clip(np.searchsorted(knots, points, side="right") - 1, degree, count - 1)
    basis = np.zeros((*points.shape, degree + 1), float)
    basis[..., 0] = 1
    left = np.zeros_like(basis)
    right = np.zeros_like(basis)
    for j in range(1, degree + 1):
        left[..., j] = points - knots[span + 1 - j]
        right[..., j] = knots[span + j] - points
        saved = np.zeros_like(points)
        for r in range(j):
            denominator = right[..., r + 1] + left[..., j - r]
            ratio = np.divide(
                basis[..., r], denominator, out=np.zeros_like(points), where=denominator != 0
            )
            basis[..., r] = saved + right[..., r + 1] * ratio
            saved = left[..., j - r] * ratio
        basis[..., j] = saved
    return span[..., None] - degree + np.arange(degree + 1), basis


def evaluate_many(model, targets, origin, shape, chunk=2048, device=None):
    dev = torch.device(device or selected_device())
    dtype = torch.float64 if dev.type == "cpu" else torch.float32
    tx, ty = model.interpolate.get_knots()
    kx, ky = model.interpolate.degrees
    coeff = np.asarray(model.interpolate.get_coeffs()).reshape(len(tx) - kx - 1, len(ty) - ky - 1)
    c = torch.tensor(coeff, device=dev, dtype=dtype)
    row, column = np.asarray(targets, float).T
    r1, r2 = int(np.floor(model.PRFrow[0])), int(np.ceil(model.PRFrow[-1]))
    c1, c2 = int(np.floor(model.PRFcol[0])), int(np.ceil(model.PRFcol[-1]))
    result = np.empty((len(row), *shape), float)
    with torch.no_grad():
        for start in range(0, len(row), chunk):
            rr = row[start : start + chunk]
            cc = column[start : start + chunk]
            n = len(rr)
            dr = np.arange(r1, r2)[None] - rr[:, None] % 1
            dc = np.arange(c1, c2)[None] - cc[:, None] % 1
            ir, br = basis_weights(tx, dr, kx)
            ic, bc = basis_weights(ty, dc, ky)
            ir = torch.tensor(ir, device=dev)
            ic = torch.tensor(ic, device=dev)
            br = torch.tensor(br, device=dev, dtype=dtype)
            bc = torch.tensor(bc, device=dev, dtype=dtype)
            values = torch.zeros((n, r2 - r1, c2 - c1), device=dev, dtype=dtype)
            for j in range(kx + 1):
                for k in range(ky + 1):
                    values += (
                        c[ir[:, :, j, None], ic[:, None, :, k]]
                        * br[:, :, j, None]
                        * bc[:, None, :, k]
                    )
            values = torch.where(values.abs() < 1e-6, torch.zeros_like(values), values)
            values /= values.sum(dim=(1, 2))[:, None, None]
            # Copy only the finite requested stamp back to the CPU.
            ri = origin[0] + np.arange(shape[0])[None] - np.floor(rr[:, None]).astype(int) - r1
            ci = origin[1] + np.arange(shape[1])[None] - np.floor(cc[:, None]).astype(int) - c1
            valid = (
                (ri[:, :, None] >= 0)
                & (ri[:, :, None] < r2 - r1)
                & (ci[:, None, :] >= 0)
                & (ci[:, None, :] < c2 - c1)
            )
            ri = torch.tensor(np.clip(ri, 0, r2 - r1 - 1), device=dev)
            ci = torch.tensor(np.clip(ci, 0, c2 - c1 - 1), device=dev)
            stamp = values[
                torch.arange(n, device=dev)[:, None, None], ri[:, :, None], ci[:, None, :]
            ]
            stamp = torch.where(torch.tensor(valid, device=dev), stamp, torch.zeros_like(stamp))
            result[start : start + n] = stamp.cpu().numpy()
    return result


def tess_pixel_model(x, y, metadata, radius=3, device=None):
    return grouped_render(
        x,
        y,
        metadata,
        radius,
        evaluator=lambda m, t, o, s: evaluate_many(m, t, o, s, device=device),
    )
