"""Prospective certified selector with fixed, score-independent runtime audits.

The compact metric still certifies every suppression decision in the finite
bank. The unchanged Metal kernel is checked against the original selector on
the first and last batch of every distance slice, for every sign and ranker.
This reduces duplicate oracle work; it is NOT an exhaustive device replay.
Runtime failures reject the result instead of silently changing selectors.
"""

import hashlib

import numpy as np
import torch

from ..runtime import device
from . import hough as multimodal
from .compact_motion_metric import certified_coordinates
from .fused_motion_reservoir import fused_retain_distinct
from .hypothesis_reservoir import motion_embeddings, retain_distinct


def audit_schedule(slice_counts, signs):
    """One-based merge indices, fixed before inspecting any image scores."""
    if (
        not slice_counts
        or any(type(n) is not int or n <= 0 for n in slice_counts)
        or tuple(signs) not in [(1.0, -1.0), (1.0,)]
    ):
        raise ValueError("Positive slice sizes and declared sign order required")
    width = 3 * len(signs)
    cursor = 0
    audits = set()
    for count in slice_counts:
        batches = (count + 31) // 32
        for batch in {0, batches - 1}:
            audits.update(range(cursor + batch * width + 1, cursor + (batch + 1) * width + 1))
        cursor += batches * width
    return cursor, sorted(audits)


class AuditGate:
    def __init__(self):
        self.completed = 0
        self.failed = False
        self.active = None

    def begin(self, orientation, signs, slice_counts):
        if self.failed or self.active is not None:
            raise RuntimeError("Selector audit failed or already active")
        expected_arm = (
            [(1.0, (1.0, -1.0)), (-1.0, (1.0,))][self.completed]
            if self.completed < 2
            else (1.0, (1.0,))
        )
        if (float(orientation), tuple(signs)) != expected_arm:
            raise RuntimeError(
                "Real positive/negative and reversed baselines must precede recovery"
            )
        expected, audits = audit_schedule(slice_counts, signs)
        self.active = dict(
            orientation=float(orientation),
            signs=list(signs),
            expected_calls=expected,
            calls=0,
            audited_calls=audits,
            verified_calls=[],
            audited_slots=0,
        )
        self.scheduled = set(audits)

    def next(self):
        if self.failed or self.active is None:
            raise RuntimeError("No active successful audit")
        self.active["calls"] += 1
        if self.active["calls"] > self.active["expected_calls"]:
            self.failed = True
            raise RuntimeError("More selector merges than declared")
        return self.active["calls"] in self.scheduled

    def compare(self, reference, accelerated):
        if (
            self.failed
            or self.active is None
            or self.active["calls"] not in self.scheduled
            or self.active["calls"] in self.active["verified_calls"]
        ):
            raise RuntimeError("Unexpected or repeated selector audit")
        if any(
            a.shape != b.shape or a.dtype != b.dtype or not torch.equal(a, b)
            for a, b in zip(reference, accelerated, strict=True)
        ):
            self.failed = True
            raise RuntimeError("Audited selector differs from original")
        self.active["verified_calls"].append(self.active["calls"])
        self.active["audited_slots"] += reference[0].numel()

    def finish(self):
        if self.failed or self.active is None:
            raise RuntimeError("No successful selector audit to finish")
        if (
            self.active["calls"] != self.active["expected_calls"]
            or self.active["verified_calls"] != self.active["audited_calls"]
        ):
            self.failed = True
            raise RuntimeError("Incomplete selector merge or audit accounting")
        record = self.active
        self.active = None
        self.completed += 1
        return record


class AuditedHoughSearch:
    def __init__(self, grid, report):
        self.grid = dict(grid)
        self.report = report
        self.original_search = multimodal.search
        self.gate = AuditGate()
        self.fingerprint = None
        self.compact = None
        self.certificate = None

    def __call__(
        self,
        sectors,
        prepared,
        specs,
        orientation=1.0,
        signs=(1.0,),
        maximum_locations=24,
        hypotheses=8,
        progress=None,
        *,
        sector_sums,
    ):
        if self.gate.failed:
            raise RuntimeError("Earlier selector failure remains latched")
        if hypotheses != 8:
            raise ValueError("Certified implementation requires eight alternatives")
        motions = np.concatenate(
            [np.c_[s["angular"], np.full(len(s["angular"]), s["inverse_distance"])] for s in specs]
        )
        full = motion_embeddings(motions, sectors).astype(np.float32)
        digest = hashlib.sha256(motions.tobytes() + full.tobytes()).hexdigest()
        if self.fingerprint is None:
            self.fingerprint = digest
            inverse = np.array([s["inverse_distance"] for s in specs])
            if len(inverse) < 2 or not np.allclose(
                np.diff(inverse), np.diff(inverse)[0], rtol=1e-10, atol=0
            ):
                raise ValueError("Uniform inverse-distance lattice required")
            compact, self.certificate = certified_coordinates(
                motions,
                full,
                motion_embeddings(np.eye(3), sectors),
                np.array([0.0, 0.0, inverse[0]]),
                np.array(
                    [self.grid["joint_angular_step_rad_per_day"]] * 2 + [inverse[1] - inverse[0]]
                ),
            )
            if self.certificate["certified"] and device().type == "mps":
                self.compact = torch.from_numpy(compact).to("mps")
            self.report("selector_metric_certificate.json", self.certificate)
        elif digest != self.fingerprint:
            raise RuntimeError("Geometry or motion bank changed after selector certification")
        if self.compact is None:
            result, details = self.original_search(
                sectors,
                prepared,
                specs,
                orientation,
                signs,
                maximum_locations,
                hypotheses,
                progress,
                sector_sums=sector_sums,
            )
            return result, dict(details, fast_selector=False, selector_fallback=True)
        self.gate.begin(orientation, signs, [len(s["angular"]) for s in specs])
        embeddings_checked = False

        def selector(scores, ids, embeddings, count=8, minimum_rms=1.5):
            nonlocal embeddings_checked
            if count != 8 or minimum_rms != 1.5:
                raise RuntimeError("Suppression settings differ from certificate")
            if not embeddings_checked:
                np.testing.assert_array_equal(embeddings.detach().cpu().numpy(), full * orientation)
                embeddings_checked = True
            audit = self.gate.next()
            accelerated = fused_retain_distinct(scores, ids, self.compact, count, minimum_rms)
            if audit:
                self.gate.compare(
                    retain_distinct(scores, ids, embeddings, count, minimum_rms), accelerated
                )
            return accelerated

        try:
            result, details = self.original_search(
                sectors,
                prepared,
                specs,
                orientation,
                signs,
                maximum_locations,
                hypotheses,
                progress,
                sector_sums=sector_sums,
                selector=selector,
            )
            if not embeddings_checked:
                raise RuntimeError("Selector was never called")
            record = self.gate.finish()
            self.report(
                f"selector_search_{self.gate.completed:03d}.json",
                dict(
                    record,
                    motion_geometry_sha256=self.fingerprint,
                    injection_truth_used=False,
                    exhaustive_device_comparison=False,
                    exact_audited_scores_and_ids=True,
                    audit_rule="first_and_last_batch_every_slice_sign_ranker",
                ),
            )
            return result, dict(
                details,
                fast_selector=True,
                selector_paired_replay=False,
                selector_runtime_audits=len(record["verified_calls"]),
                selector_certified_with_fixed_audits=True,
            )
        except Exception as error:
            self.gate.failed = True
            self.report(
                "selector_failure.json", dict(error=repr(error), fast_backend_rejected=True)
            )
            raise
