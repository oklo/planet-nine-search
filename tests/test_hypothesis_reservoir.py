import unittest

import torch

from p9search.science.hypothesis_reservoir import retain_distinct


class HypothesisReservoirTests(unittest.TestCase):
    def test_true_orbit_survives_higher_competing_link(self):
        # Many nearby fine-grid samples describe the stronger false orbit.
        # A plain top-K would fill with those samples and lose the real orbit.
        coordinates = torch.tensor([[i * 0.02, 0.0] for i in range(20)] + [[8.0, 0.0]])
        scores = torch.tensor([80.9 - i * 0.01 for i in range(20)] + [79.14])[:, None, None]
        ids = torch.arange(21)[:, None, None]
        values, kept = retain_distinct(scores, ids, coordinates, count=2, minimum_rms=1.5)
        self.assertEqual(kept[:, 0, 0].tolist(), [0, 20])
        self.assertAlmostEqual(float(values[1, 0, 0]), 79.14, places=4)

    def test_stream_merge_keeps_separate_pixel_hypotheses(self):
        embed = torch.tensor([[0.0, 0.0], [0.1, 0.0], [5.0, 0.0], [10.0, 0.0]])
        first = torch.tensor([[[8.0, 2.0]], [[7.0, 9.0]]])
        ids = torch.tensor([[[0, 0]], [[2, 2]]])
        old, old_ids = retain_distinct(first, ids, embed, count=3)
        new = torch.tensor([[[10.0, 3.0]], [[6.0, 5.0]]])
        new_ids = torch.tensor([[[1, 1]], [[3, 3]]])
        val, kept = retain_distinct(
            torch.cat([old, new]), torch.cat([old_ids, new_ids]), embed, count=3
        )
        self.assertEqual(kept[:, 0, 0].tolist(), [1, 2, 3])
        self.assertEqual(kept[:, 0, 1].tolist(), [2, 3, 1])

    def test_invalid_entries_never_create_hypotheses(self):
        score = torch.tensor([[[float("nan")]], [[5.0]]])
        ids = torch.tensor([[[-1]], [[0]]])
        values, kept = retain_distinct(score, ids, torch.tensor([[0.0, 0.0]]), count=3)
        self.assertEqual(kept[:, 0, 0].tolist(), [0, -1, -1])


if __name__ == "__main__":
    unittest.main()
