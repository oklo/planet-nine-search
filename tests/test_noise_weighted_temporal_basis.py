import unittest

import numpy as np

from p9search.science.noise_weighted_temporal_basis import learn_noise_weighted_basis


class NoiseWeightedBasisTests(unittest.TestCase):
    def fixture(self):
        rng = np.random.default_rng(69128)
        data = rng.normal(size=(45, 24, 24))
        u = rng.uniform(0.2, 3, 45)
        v = rng.uniform(0.2, 3, (24, 24))
        keep = np.ones(45, bool)
        keep[3] = False
        u[7] = 0
        v[0, 0] = 0
        return data, keep, u, v

    def fit(self, *args):
        return learn_noise_weighted_basis(*args, n_modes=3, tile_size=6)

    def test_weighted_orthonormal_and_zero_exposure_rows(self):
        data, keep, u, v = self.fixture()
        b, _ = self.fit(data, keep, u, v)
        for basis in b:
            np.testing.assert_allclose(basis.T @ (u[:, None] * basis), np.eye(4), atol=1e-12)
        self.assertTrue((b[:, [3, 7]] == 0).all())
        np.testing.assert_allclose(b[0, keep & (u > 0), 0], np.sum(u[keep & (u > 0)]) ** -0.5)

    def test_target_fold_cannot_train_its_own_basis(self):
        data, keep, u, v = self.fixture()
        b, fold = self.fit(data, keep, u, v)
        changed = data.copy()
        changed[:, fold == 2] += np.linspace(0, 100, 45)[:, None]
        c, _ = self.fit(changed, keep, u, v)
        np.testing.assert_array_equal(b[2], c[2])

    def test_units_and_separable_weight_factorization(self):
        data, keep, u, v = self.fixture()
        b, _ = self.fit(data, keep, u, v)
        units, _ = self.fit(data * 8, keep, u, v / 64)
        split, _ = self.fit(data, keep, u * 4, v / 4)
        np.testing.assert_allclose(b, units, atol=1e-12)
        np.testing.assert_allclose(b, split * 2, atol=1e-12)

    def test_masked_pixels_do_not_train_any_modes(self):
        data, keep, u, v = self.fixture()
        b, _ = self.fit(data, keep, u, v)
        changed = data.copy()
        changed[:, 0, 0] = np.nan
        c, _ = self.fit(changed, keep, u, v)
        np.testing.assert_array_equal(b, c)

    def test_seed_is_reproducible(self):
        args = self.fixture()
        b, f = self.fit(*args)
        c, g = self.fit(*args)
        np.testing.assert_array_equal(b, c)
        np.testing.assert_array_equal(f, g)

    def test_invalid_inputs_rejected(self):
        data, keep, u, v = self.fixture()
        with self.assertRaises(ValueError):
            self.fit(data, keep, -u, v)
        with self.assertRaises(ValueError):
            self.fit(data, keep, np.zeros_like(u), v)
        with self.assertRaises(ValueError):
            self.fit(data, keep, u, np.zeros_like(v))
