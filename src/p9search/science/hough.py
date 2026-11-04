"""Joint-sector proposals retaining distinct orbits at each sky neighborhood."""

import numpy as np
import torch
from torch.nn import functional as F

from .. import runtime as bs
from .geometry import combine_sector_maps
from .hypothesis_reservoir import motion_embeddings, retain_distinct


def search(
    sectors,
    prepared,
    specs,
    orientation=1.0,
    signs=(1.0,),
    maximum_locations=192,
    hypotheses=8,
    progress=None,
    *,
    sector_sums,
    selector=retain_distinct,
):
    """Select sky neighborhoods, then retain distinct orbit alternatives.

    Neighborhood gathering includes adjacent anchors so the position maximum
    of a competing orbit cannot hide a valid orbit one pixel away. The larger
    hypothesis budget is explicit; this is not a matched-budget comparison.
    """
    dev = bs.device()
    h = w = 140
    methods = ["formal", "contrast", "coherent"]
    motions = np.concatenate(
        [np.c_[s["angular"], np.full(len(s["angular"]), s["inverse_distance"])] for s in specs]
    )
    embedding_np = motion_embeddings(motions, sectors) * orientation
    embeddings = torch.tensor(embedding_np, dtype=torch.float32, device=dev)
    best = {
        (s, m): torch.full((hypotheses, h, w), -1e9, device=dev) for s in signs for m in methods
    }
    ids = {key: torch.full((hypotheses, h, w), -1, dtype=torch.long, device=dev) for key in best}
    base = [torch.tensor(s.base_grid, dtype=torch.float32, device=dev) for s in sectors]
    cursor = 0
    with torch.no_grad():
        for di, spec in enumerate(specs):
            g = spec["inverse_distance"]
            fine = spec["angular"]
            local = spec["local_angular"]
            banks = [
                sector_sums(p["maps"], p["coarse"], s, local, g, p["psf"], orientation)
                for s, p in zip(sectors, prepared)
            ]
            for start in range(0, len(fine), 32):
                q = fine[start : start + 32]
                b = len(q)
                labels = torch.tensor(
                    spec["labels"][start : start + b], dtype=torch.long, device=dev
                )
                offsets = [
                    torch.tensor(
                        (q @ s.jacobian.T * (s.epoch - s.frame.reference_time) + s.offset * g)
                        * orientation,
                        dtype=torch.float32,
                        device=dev,
                    )
                    for s in sectors
                ]
                score = combine_sector_maps(banks, base, offsets, labels)
                new_ids = torch.arange(cursor + start, cursor + start + b, device=dev)[
                    :, None, None
                ].expand(-1, h, w)
                for sign in signs:
                    for method in methods:
                        value = (
                            sign * score[method]
                            if method != "coherent"
                            else sign * score["formal"] - score["chi_sqrt"]
                        )
                        peak = value == F.max_pool2d(value[:, None], 5, 1, 2)[:, 0]
                        value = torch.where(
                            score["valid"] & peak, value, torch.full_like(value, -1e9)
                        )
                        key = (sign, method)
                        best[key], ids[key] = selector(
                            torch.cat([best[key], value]),
                            torch.cat([ids[key], new_ids]),
                            embeddings,
                            count=hypotheses,
                            minimum_rms=1.5,
                        )
            cursor += len(fine)
            del banks
            if progress:
                progress(di + 1, len(specs), cursor)
    results = {}
    for sign in signs:
        values = {m: best[(sign, m)].cpu().numpy() for m in methods}
        identifiers = {m: ids[(sign, m)].cpu().numpy() for m in methods}
        queues = {
            m: [
                np.unravel_index(i, (h, w))
                for i in np.argsort(values[m][0].ravel())[::-1]
                if identifiers[m][0].ravel()[i] >= 0
            ]
            for m in methods
        }
        locations = []
        cursors = {m: 0 for m in methods}
        while len(locations) < maximum_locations:
            changed = False
            for method in methods:
                while cursors[method] < len(queues[method]):
                    y, x = queues[method][cursors[method]]
                    cursors[method] += 1
                    if (
                        locations
                        and np.min(np.linalg.norm(np.asarray(locations) - [x, y], axis=1)) < 2
                    ):
                        continue
                    locations.append([x, y])
                    changed = True
                    break
                if len(locations) >= maximum_locations:
                    break
            if not changed:
                break
        proposals = []
        for location_rank, (x, y) in enumerate(locations):
            alternatives = {}
            for method in methods:
                rows = []
                for yy in range(max(0, y - 1), min(h, y + 2)):
                    for xx in range(max(0, x - 1), min(w, x + 2)):
                        for slot in range(hypotheses):
                            idx = int(identifiers[method][slot, yy, xx])
                            if idx >= 0:
                                rows.append((float(values[method][slot, yy, xx]), xx, yy, idx))
                alternatives[method] = sorted(rows, reverse=True)
            selected = []
            signatures = []
            position = {m: 0 for m in methods}
            while len(selected) < hypotheses:
                changed = False
                for method in methods:
                    while position[method] < len(alternatives[method]):
                        value, xx, yy, idx = alternatives[method][position[method]]
                        position[method] += 1
                        signature = embedding_np[idx] + np.tile([xx, yy], len(sectors)) / np.sqrt(
                            len(sectors)
                        )
                        if (
                            signatures
                            and np.min(np.linalg.norm(np.asarray(signatures) - signature, axis=1))
                            < 1.5
                        ):
                            continue
                        signatures.append(signature)
                        selected.append(
                            dict(
                                parameters=np.r_[xx, yy, motions[idx]],
                                proposal_method=method,
                                proposal_score=value,
                                sign=sign,
                                orientation=orientation,
                                location_rank=location_rank + 1,
                                alternative_rank=len(selected) + 1,
                            )
                        )
                        changed = True
                        break
                    if len(selected) >= hypotheses:
                        break
                if not changed:
                    break
            proposals.extend(selected)
        results[sign] = proposals
    return results, dict(
        fine_motion_trials=len(motions),
        maximum_sky_locations=maximum_locations,
        hypotheses_per_location=hypotheses,
        maximum_hypotheses=maximum_locations * hypotheses,
        candidates_per_sign={str(s): len(rows) for s, rows in results.items()},
        selection="Multiple distinct motion hypotheses and neighboring anchors per selected sky region",
    )
