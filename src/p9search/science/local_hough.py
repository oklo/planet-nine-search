"""Local covariance sufficient statistics for the shared multi-sector Hough grid."""

import numpy as np
import torch
from torch.nn import functional as F

from .. import runtime as bs
from .covariance_hough import CovarianceInformationBank


def sector_sums(bundle, coarse, sector, q, g, psf, orientation=1.0):
    dev = bs.device()
    maps = bundle["numerator"]
    t, h, w = maps.shape
    motions = np.c_[
        (q @ sector.jacobian.T + sector.slope * g) * orientation, np.full(len(q), g * orientation)
    ]
    offsets = (
        coarse["dt"][None, :, None] * motions[:, None, :2]
        + coarse["curvature"][None] * motions[:, None, 2:]
        + coarse["pointing"][None]
    )
    maximum = float(np.max(np.abs(offsets)))
    if "bank" not in bundle or bundle["bank"].max_offset < maximum:
        scene = bundle["scene"]
        bundle["bank"] = CovarianceInformationBank(
            bundle["temporal"],
            scene.time_weight,
            scene.pixel_weight,
            bundle["groups"],
            psf,
            max_offset=float(np.ceil(maximum) + 0.5),
        )
    tensor = torch.tensor(maps[:, None], device=dev)
    normal = torch.tensor([w - 1, h - 1], device=dev)
    yy, xx = torch.meshgrid(torch.arange(h, device=dev), torch.arange(w, device=dev), indexing="ij")
    origin = torch.stack([xx, yy], dim=-1)
    result = []
    with torch.no_grad():
        for start in range(0, len(q), 4):
            positions = offsets[start : start + 4]
            b = len(positions)
            information = [bundle["bank"].information(p) for p in positions]
            denominator = torch.tensor(
                np.stack([r["information"] for r in information]), device=dev
            )
            raw = torch.tensor(
                np.stack([r["unprofiled_information"] for r in information]), device=dev
            )
            pos = (
                origin[None, None]
                + torch.tensor(positions.transpose(1, 0, 2), dtype=torch.float32, device=dev)[
                    :, :, None, None
                ]
            )
            inside = (
                (pos[..., 0] >= 7)
                & (pos[..., 0] <= w - 8)
                & (pos[..., 1] >= 7)
                & (pos[..., 1] <= h - 8)
            ).all(dim=0)
            valid = inside & (denominator > torch.maximum(raw * 1e-5, torch.full_like(raw, 1e-20)))
            numerator = (
                F.grid_sample(
                    tensor, (2 * pos / normal - 1).reshape(t, b * h, w, 2), align_corners=True
                )
                .sum(dim=0)
                .reshape(b, h, w)
            )
            result.append(torch.stack([numerator, denominator], dim=1) * valid[:, None])
    return torch.cat(result)
