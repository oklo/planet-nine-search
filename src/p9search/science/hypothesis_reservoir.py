"""Retain distinct orbit hypotheses at each reference-image position.

One motion per pixel loses real tracks when an unrelated source at a later
epoch creates a slightly stronger competing link. This streaming reservoir
keeps multiple geometrically distinct alternatives, without storing the volume.
"""

import numpy as np
import torch


def motion_embeddings(motions, sectors):
    """Euclidean embedding whose distance is RMS displacement across epochs."""
    motions = np.asarray(motions)
    frame = sectors[0].frame
    reference = next(s for s in sectors if s.meta["sector"] == frame.meta["sector"])
    values = []
    for sector in sectors:
        dt = sector.epoch - frame.reference_time
        reflex = -(sector.observer(sector.epoch) - frame.observer) @ frame.tangent.T
        offsets = (motions[:, :2] * dt + motions[:, 2, None] * reflex) @ reference.jacobian.T
        values.append(offsets)
    return np.concatenate(values, axis=1) / np.sqrt(len(sectors))


def retain_distinct(scores, identifiers, embeddings, count=8, minimum_rms=1.5):
    """Greedy score ordering with orbit-distance suppression at each pixel.

    Merge old reservoir entries and a new motion batch before calling. The
    greedy streaming approximation remains order-dependent; its completeness
    must be measured with native injections. Empty slots have identifier -1.
    """
    if scores.shape != identifiers.shape or scores.ndim != 3:
        raise ValueError("Expected matching hypothesis,y,x arrays")
    if count < 1 or minimum_rms <= 0:
        raise ValueError("Invalid reservoir settings")
    valid = (identifiers >= 0) & torch.isfinite(scores) & (scores > -1e8)
    available = torch.where(valid, scores, torch.full_like(scores, -1e9))
    coordinates = embeddings[identifiers.clamp_min(0)]
    kept_scores = []
    kept_ids = []
    for _ in range(count):
        value, index = available.max(dim=0)
        chosen = torch.gather(identifiers, 0, index[None])[0]
        occupied = value > -1e8
        chosen = torch.where(occupied, chosen, torch.full_like(chosen, -1))
        kept_scores.append(value)
        kept_ids.append(chosen)
        selected = embeddings[chosen.clamp_min(0)]
        distance = ((coordinates - selected[None]) ** 2).sum(dim=-1)
        suppress = (distance < minimum_rms**2) & occupied[None]
        available = torch.where(suppress, torch.full_like(available, -1e9), available)
    return torch.stack(kept_scores), torch.stack(kept_ids)
