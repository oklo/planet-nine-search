import unittest
from unittest.mock import patch

import numpy as np
import torch

from p9search.science import audited_hough_selector as module
from p9search.science import hough as multimodal


class AuditGateTests(unittest.TestCase):
    def values(self):
        return torch.ones((8, 2, 3)), torch.zeros((8, 2, 3), dtype=torch.long)

    def complete(self, gate, orientation, signs):
        gate.begin(orientation, signs, [1, 33, 96])
        while gate.active["calls"] < gate.active["expected_calls"]:
            if gate.next():
                gate.compare(self.values(), self.values())
        return gate.finish()

    def test_schedule_accounts_for_short_slices_and_partial_final_batches(self):
        n, indices = module.audit_schedule([1, 33, 96], (1.0, -1.0))
        self.assertEqual(n, 36)
        self.assertEqual(indices, list(range(1, 25)) + list(range(31, 37)))
        self.assertEqual(module.audit_schedule([96], (1.0,)), (9, [1, 2, 3, 7, 8, 9]))
        for counts in [[], [0], [1.5]]:
            with self.assertRaises(ValueError):
                module.audit_schedule(counts, (1.0,))

    def test_real_controls_precede_recovery_and_all_merges_are_counted(self):
        gate = module.AuditGate()
        with self.assertRaises(RuntimeError):
            gate.begin(1.0, (1.0,), [96])
        first = self.complete(gate, 1.0, (1.0, -1.0))
        self.assertEqual(first["calls"], 36)
        self.assertEqual(first["audited_slots"], 30 * 48)
        self.complete(gate, -1.0, (1.0,))
        self.complete(gate, 1.0, (1.0,))
        self.assertEqual(gate.completed, 3)

    def test_score_and_identifier_faults_latch_failure(self):
        for position in [0, 1]:
            gate = module.AuditGate()
            gate.begin(1.0, (1.0, -1.0), [96])
            self.assertTrue(gate.next())
            changed = self.values()
            changed[position][0, 0, 0] += 1
            with self.assertRaisesRegex(RuntimeError, "differs"):
                gate.compare(self.values(), changed)
            for operation in [gate.next, gate.finish]:
                with self.assertRaises(RuntimeError):
                    operation()

    def test_missing_and_duplicate_audits_or_extra_merges_cannot_pass(self):
        gate = module.AuditGate()
        gate.begin(1.0, (1.0, -1.0), [1])
        for _ in range(6):
            gate.next()
        with self.assertRaisesRegex(RuntimeError, "Incomplete"):
            gate.finish()
        gate = module.AuditGate()
        gate.begin(1.0, (1.0, -1.0), [1])
        gate.next()
        gate.compare(self.values(), self.values())
        with self.assertRaisesRegex(RuntimeError, "repeated"):
            gate.compare(self.values(), self.values())
        for _ in range(5):
            gate.next()
        with self.assertRaisesRegex(RuntimeError, "More"):
            gate.next()

    def test_complete_wrapper_audits_fixed_merges_and_preserves_outputs(self):
        specs = [
            dict(angular=np.arange(192, dtype=float).reshape(96, 2) / 1000, inverse_distance=g)
            for g in [0.001, 0.002]
        ]
        records = {}
        oracle_calls = []
        from_numpy = torch.from_numpy
        original_selector = module.retain_distinct

        def reference(*args, **kwargs):
            oracle_calls.append(1)
            return original_selector(*args, **kwargs)

        def fast(scores, ids, coordinates, count, minimum_rms):
            return original_selector(scores, ids, coordinates, count, minimum_rms)

        class Upload:
            def __init__(self, array):
                self.array = array

            def to(self, device):
                return from_numpy(self.array)

        def search(sectors, prepared, bank, orientation, signs, *args, sector_sums, selector=None):
            motions = np.concatenate(
                [
                    np.c_[s["angular"], np.full(len(s["angular"]), s["inverse_distance"])]
                    for s in bank
                ]
            )
            embeddings = from_numpy((motions * orientation).astype(np.float32))
            scores = torch.arange(24, dtype=torch.float32).reshape(4, 2, 3)
            ids = torch.arange(4)[:, None, None].expand(4, 2, 3)
            n, _ = module.audit_schedule([len(s["angular"]) for s in bank], signs)
            for _ in range(n):
                actual = selector(scores, ids, embeddings)
                expected = original_selector(scores, ids, embeddings)
                for a, b in zip(actual, expected):
                    self.assertTrue(torch.equal(a, b))
            return {"unchanged": True}, {}

        def certificate(motions, full, *args):
            return full, dict(certified=True)

        with (
            patch.object(multimodal, "search", side_effect=search),
            patch.object(module, "motion_embeddings", side_effect=lambda m, s: np.asarray(m)),
            patch.object(module, "certified_coordinates", side_effect=certificate),
            patch.object(module, "device", return_value=torch.device("mps")),
            patch.object(module.torch, "from_numpy", side_effect=Upload),
            patch.object(module, "retain_distinct", side_effect=reference),
            patch.object(module, "fused_retain_distinct", side_effect=fast),
        ):
            backend = module.AuditedHoughSearch(
                dict(joint_angular_step_rad_per_day=0.001),
                lambda key, value: records.update({key: value}),
            )
            for orientation, signs in [(1.0, (1.0, -1.0)), (-1.0, (1.0,)), (1.0, (1.0,))]:
                result, details = backend(
                    [], [], specs, orientation=orientation, signs=signs, sector_sums=None
                )
                self.assertTrue(result["unchanged"])
                self.assertTrue(details["selector_certified_with_fixed_audits"])
            self.assertEqual(len(oracle_calls), 48)
            self.assertEqual(
                sum(r["calls"] for k, r in records.items() if k.startswith("selector_search")), 72
            )
            changed = [dict(s) for s in specs]
            changed[0]["angular"] = changed[0]["angular"] + 0.01
            with self.assertRaisesRegex(RuntimeError, "bank changed"):
                backend([], [], changed, sector_sums=None)


if __name__ == "__main__":
    unittest.main()


def test_older_torch_without_metal_compiler_uses_reference_selector(monkeypatch):
    monkeypatch.delattr(torch.mps, "compile_shader", raising=False)
    monkeypatch.setattr(module, "device", lambda: torch.device("mps"))
    monkeypatch.setattr(module, "motion_embeddings", lambda motions, sectors: np.asarray(motions))
    monkeypatch.setattr(
        module, "certified_coordinates", lambda motions, full, *args: (full, {"certified": True})
    )
    monkeypatch.setattr(multimodal, "search", lambda *args, **kwargs: ({"reference": True}, {}))
    specs = [dict(angular=np.zeros((1, 2)), inverse_distance=g) for g in [0.001, 0.002]]
    backend = module.AuditedHoughSearch(
        {"joint_angular_step_rad_per_day": 0.001}, lambda *args: None
    )
    result, details = backend([], [], specs, sector_sums=None)
    assert result == {"reference": True}
    assert details["selector_fallback"]
    assert not details["fast_selector"]
