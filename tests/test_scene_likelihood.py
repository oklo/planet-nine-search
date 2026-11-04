import unittest

import numpy as np

from p9search.science.scene_likelihood import SceneProjector


class SceneLikelihoodTest(unittest.TestCase):
    def test_full_joint_nuisance_projection(self):
        rng = np.random.default_rng(52)
        t, h, w, k = 11, 3, 4, 3
        pixels = h * w
        a = rng.normal(size=(pixels, k))
        data = rng.normal(size=(t, h, w))
        p = rng.uniform(size=data.shape)
        u = rng.uniform(0.1, 2, t)
        v = rng.uniform(0.1, 2, (h, w))
        weight = u[:, None, None] * v
        # Build the redundant full nuisance system; use SVD to retain its true rank.
        nuisance = np.column_stack([np.tile(np.eye(pixels), (t, 1)), np.kron(np.eye(t), a)])
        weighted = nuisance * np.sqrt(weight.ravel())[:, None]
        left, s, _ = np.linalg.svd(weighted, full_matrices=False)
        q = left[:, s > s.max() * 1e-10]
        pp = p.ravel() * np.sqrt(weight.ravel())
        yy = data.ravel() * np.sqrt(weight.ravel())
        pp -= q @ (q.T @ pp)
        info = pp @ pp
        expected = pp @ yy / info
        result = SceneProjector(data, u, v, a).score(p)
        self.assertAlmostEqual(result["information"], info, places=10)
        self.assertAlmostEqual(result["flux"], expected, places=10)

    def test_nuisance_signal_not_counted_as_object(self):
        rng = np.random.default_rng(5)
        t, h, w = 20, 4, 4
        a = rng.normal(size=(h * w, 2))
        u = np.ones(t)
        v = np.ones((h, w))
        coeff = rng.normal(size=(t, 2))
        sky = rng.normal(size=h * w)
        data = (coeff @ a.T + sky).reshape(t, h, w)
        template = rng.uniform(size=data.shape)
        result = SceneProjector(data, u, v, a).score(template)
        self.assertAlmostEqual(result["flux"], 0.0, places=12)
        profile = SceneProjector(data - 3 * template, u, v, a).score(template)
        self.assertAlmostEqual(profile["flux"], -3.0, places=12)

    def test_sparse_region_equals_full_template(self):
        rng = np.random.default_rng(1)
        data = rng.normal(size=(12, 7, 8))
        a = rng.normal(size=(56, 4))
        projection = SceneProjector(data, np.ones(12), np.ones((7, 8)), a)
        p = rng.uniform(size=(12, 3, 4))
        full = np.zeros_like(data)
        full[:, 2:5, 1:5] = p
        one = projection.score(p, (slice(None), slice(2, 5), slice(1, 5)))
        two = projection.score(full)
        for key in ["information", "flux", "sigma"]:
            self.assertAlmostEqual(one[key], two[key], places=11)


if __name__ == "__main__":
    unittest.main()
