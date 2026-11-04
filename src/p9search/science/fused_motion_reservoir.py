"""Metal reservoir for certified three-coordinate motion banks.

The caller must certify the compact metric,
keep identifiers in the bank's range (or negative for empty slots), and preserve
the original batch/score order. Whole-search replay remains an adoption gate.
"""

import math
from functools import lru_cache

import torch

SOURCE = r"""
#include <metal_stdlib>
using namespace metal;

kernel void distinct_motion_reservoir(
    device const float* scores,
    device const int* identifiers,
    device const float* coordinates,
    device float* kept_scores,
    device int* kept_identifiers,
    constant int* dimensions,
    constant float& threshold_squared,
    uint pixel [[thread_position_in_grid]]) {
    const int n = dimensions[0];
    const int pixels = dimensions[1];
    const int count = dimensions[2];
    const int motions = dimensions[3];
    if (pixel >= uint(pixels)) return;
    float available[40];
    int ids[40];
    for (int i = 0; i < n; ++i) {
        const int offset = i * pixels + int(pixel);
        const float value = scores[offset];
        ids[i] = identifiers[offset];
        available[i] = (ids[i] >= 0 && ids[i] < motions && isfinite(value) && value > -1.0e8f)
                     ? value : -1.0e9f;
    }
    for (int slot = 0; slot < count; ++slot) {
        float best = -1.0e9f;
        int winner = 0;
        // Strict comparison retains the first index on equal scores.
        for (int i = 0; i < n; ++i) {
            if (available[i] > best) { best = available[i]; winner = i; }
        }
        const bool occupied = best > -1.0e8f;
        const int chosen = occupied ? ids[winner] : -1;
        kept_scores[slot * pixels + int(pixel)] = best;
        kept_identifiers[slot * pixels + int(pixel)] = chosen;
        if (!occupied) continue;
        const float x = coordinates[3 * chosen];
        const float y = coordinates[3 * chosen + 1];
        const float z = coordinates[3 * chosen + 2];
        for (int i = 0; i < n; ++i) {
            if (available[i] <= -1.0e8f) continue;
            const int id = ids[i];
            const float dx = coordinates[3 * id] - x;
            const float dy = coordinates[3 * id + 1] - y;
            const float dz = coordinates[3 * id + 2] - z;
            const float distance_squared = dx * dx + dy * dy + dz * dz;
            if (distance_squared < threshold_squared) available[i] = -1.0e9f;
        }
    }
}
"""


@lru_cache(maxsize=1)
def library():
    return torch.mps.compile_shader(SOURCE)


def fused_retain_distinct(scores, identifiers, coordinates, count=8, minimum_rms=1.5):
    """Run one pixel-local kernel; ID range is a trusted search-bank invariant."""
    if (
        scores.device.type != "mps"
        or identifiers.device != scores.device
        or coordinates.device != scores.device
        or scores.dtype != torch.float32
        or coordinates.dtype != torch.float32
        or identifiers.dtype not in [torch.int32, torch.int64]
        or scores.ndim != 3
        or identifiers.shape != scores.shape
        or coordinates.ndim != 2
        or coordinates.shape[1] != 3
        or not 1 <= scores.shape[0] <= 40
        or not 1 <= count <= 8
        or not 1 <= len(coordinates) < 2**31
        or not math.isfinite(minimum_rms)
        or minimum_rms <= 0
    ):
        raise ValueError("Expected bounded MPS FP32 scores and certified three-coordinate bank")
    n, h, w = scores.shape
    if h * w == 0 or n * h * w >= 2**31:
        raise ValueError("Invalid image shape")
    values = scores.contiguous()
    ids = identifiers.to(torch.int32).contiguous()
    embedding = coordinates.contiguous()
    kept_scores = torch.empty((count, h, w), dtype=torch.float32, device=scores.device)
    kept_ids = torch.empty((count, h, w), dtype=torch.int32, device=scores.device)
    dimensions = torch.tensor(
        [n, h * w, count, len(coordinates)], dtype=torch.int32, device=scores.device
    )
    library().distinct_motion_reservoir(
        values,
        ids,
        embedding,
        kept_scores,
        kept_ids,
        dimensions,
        float(minimum_rms**2),
        threads=h * w,
        group_size=64,
    )
    return kept_scores, kept_ids.to(identifiers.dtype)
