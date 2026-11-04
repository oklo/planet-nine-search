import unittest

import numpy as np

from p9search.science.adaptive_covariance import AdaptiveTemporalProjector

from .reference import profile_stationary


class AdaptiveCovarianceTests(unittest.TestCase):
    def fixture(self):
        rng = np.random.default_rng(82)
        t = 13
        shape = (t, 3, 4)
        data = rng.normal(size=shape)
        weight = rng.uniform(0.5, 2, shape)
        p = rng.normal(size=shape)
        time = np.linspace(-1, 1, t)
        u = np.c_[np.ones(t), time, time * time]
        bases = np.repeat(u[None], 4, axis=0)
        fold = np.arange(12).reshape(3, 4) % 4
        return rng, data, weight, p, bases, fold

    def test_conditional_gls_matches_explicit_covariance(self):
        rng, data, w, p, bases, fold = self.fixture()
        lam = rng.uniform(0.1, 2, (3, 4, 2))
        lam[0, 0, 0] = 0
        projector = AdaptiveTemporalProjector(data, w, bases, fold, prior_variances=lam)
        result = projector.score(p)
        n = d = 0.0
        for y in range(3):
            for x in range(4):
                u = bases[fold[y, x], :, 1:]
                covariance = np.diag(1 / w[:, y, x]) + u @ np.diag(lam[y, x]) @ u.T
                inv = np.linalg.inv(covariance)
                one = np.ones(len(p))
                c = inv @ one
                projected = inv - np.outer(c, c) / (one @ c)
                n += p[:, y, x] @ projected @ data[:, y, x]
                d += p[:, y, x] @ projected @ p[:, y, x]
        np.testing.assert_allclose(result["numerator"], n, rtol=2e-6, atol=1e-6)
        np.testing.assert_allclose(result["information"], d, rtol=2e-6, atol=1e-6)

    def test_zero_mode_variance_equals_stationary_profile(self):
        _, data, w, p, bases, fold = self.fixture()
        projector = AdaptiveTemporalProjector(
            data, w, bases, fold, prior_variances=np.zeros((3, 4, 2))
        )
        actual = projector.score(p)
        expected = profile_stationary(data, w, p)
        for name in ["numerator", "information", "flux", "sigma"]:
            np.testing.assert_allclose(actual[name], expected[name], rtol=2e-6, atol=1e-6)

    def test_activation_is_local_and_zero_weight_is_safe(self):
        _, data, w, p, bases, fold = self.fixture()
        data[:] = 0
        w[:] = 1
        data[:, 0, 0] = 100 * bases[0, :, 1]
        w[:, 2, 3] = 0
        projector = AdaptiveTemporalProjector(data, w, bases, fold, activation_sigma=5.0)
        self.assertGreater(projector.active_counts[0, 0], 0)
        self.assertEqual(projector.active_counts[1, 1], 0)
        self.assertEqual(projector.active_counts[2, 3], 0)
        self.assertTrue(np.isfinite(projector.inverse_gram).all())
        self.assertTrue(projector.score(p)["identifiable"])


if __name__ == "__main__":
    unittest.main()
