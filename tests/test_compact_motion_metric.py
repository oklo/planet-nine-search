import unittest

import numpy as np
import torch

from p9search.science.compact_motion_metric import certified_coordinates
from p9search.science.hypothesis_reservoir import retain_distinct


class CompactMotionMetricTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(951)
        self.operator = np.linalg.qr(rng.normal(size=(10, 3)))[0].T
        axis = np.arange(-2, 3)
        self.motions = (
            np.stack(np.meshgrid(axis, axis, axis, indexing="ij"), axis=-1).reshape(-1, 3) * 0.4
        )
        self.full = (self.motions @ self.operator).astype(np.float32)

    def build(self, **changes):
        values = dict(
            motions=self.motions,
            embeddings=self.full,
            operator=self.operator,
            origin=np.zeros(3),
            step=np.full(3, 0.4),
        )
        values.update(changes)
        return certified_coordinates(**values)

    def test_all_pairs_and_greedy_outputs_agree_for_certified_bank(self):
        compact, report = self.build()
        self.assertTrue(report["certified"])
        self.assertEqual(compact.shape, (125, 3))
        old = torch.from_numpy(self.full)
        new = torch.from_numpy(compact)
        old_near = ((old[:, None] - old[None]) ** 2).sum(-1) < 2.25
        new_near = ((new[:, None] - new[None]) ** 2).sum(-1) < 2.25
        self.assertTrue(torch.equal(old_near, new_near))
        rng = np.random.default_rng(441)
        ids = torch.from_numpy(rng.integers(0, 125, size=(40, 5, 6)))
        scores = torch.from_numpy(rng.integers(-3, 4, size=(40, 5, 6)).astype(np.float32))
        scores[0, 0, 0] = float("nan")
        ids[1, 0, 0] = -1
        before = retain_distinct(scores, ids, old)
        after = retain_distinct(scores, ids, new)
        for a, b in zip(before, after):
            self.assertTrue(torch.equal(a, b))

    def test_exact_threshold_falls_back_without_changing_original_bytes(self):
        motions = np.array([[0.0, 0.0, 0.0], [1.5, 0.0, 0.0]])
        operator = np.c_[np.eye(3), np.zeros((3, 7))]
        full = (motions @ operator).astype(np.float32)
        chosen, report = self.build(
            motions=motions, embeddings=full, operator=operator, step=np.full(3, 1.5)
        )
        self.assertFalse(report["certified"])
        self.assertIn("strict suppression boundary", report["fallback_reason"])
        self.assertEqual(chosen.tobytes(), full.tobytes())

    def test_model_residual_cannot_be_ignored(self):
        changed = self.full.copy()
        changed[-1, 0] += 100
        chosen, report = self.build(embeddings=changed)
        self.assertFalse(report["certified"])
        self.assertEqual(chosen.tobytes(), changed.tobytes())

    def test_degenerate_geometry_uses_full_metric(self):
        operator = self.operator.copy()
        operator[2] = 0
        full = (self.motions @ operator).astype(np.float32)
        chosen, report = self.build(operator=operator, embeddings=full)
        self.assertFalse(report["certified"])
        self.assertEqual(chosen.tobytes(), full.tobytes())

    def test_reversed_geometry_keeps_identical_suppression(self):
        compact, report = self.build(operator=-self.operator, embeddings=-self.full)
        self.assertTrue(report["certified"])
        before = np.sum((self.full[:, None] - self.full[None]) ** 2, axis=-1) < 2.25
        after = np.sum((compact[:, None] - compact[None]) ** 2, axis=-1) < 2.25
        np.testing.assert_array_equal(before, after)

    def test_invalid_values_are_rejected(self):
        with self.assertRaises(ValueError):
            self.build(step=np.array([0.4, 0, 0.4]))
        changed = self.full.copy()
        changed[0, 0] = np.nan
        with self.assertRaises(ValueError):
            self.build(embeddings=changed)
        with self.assertRaises(ValueError):
            self.build(operator=np.ones((2, 10)))


if __name__ == "__main__":
    unittest.main()
