import unittest

import numpy as np

from p9search.science.catalog_covariance import CatalogVariancePlan, catalog_features


class CatalogCovarianceTests(unittest.TestCase):
    def test_donors_exclude_fold_and_neighborhood(self):
        yy, xx = np.indices((14, 16))
        fold = (xx // 4 % 2) + 2 * (yy // 4 % 2)
        v = np.ones(fold.shape)
        features = np.stack([xx / 16, yy / 14], axis=-1)
        plan = CatalogVariancePlan(features, fold, v, neighbors=8, exclusion_radius=3.0)
        coordinates = np.c_[xx.ravel(), yy.ravel()]
        self.assertTrue(np.all(fold.ravel()[plan.donors] != fold.ravel()[:, None]))
        self.assertTrue(
            np.all(np.linalg.norm(coordinates[plan.donors] - coordinates[:, None], axis=2) > 3)
        )

    def test_target_neighborhood_cannot_set_its_variance(self):
        rng = np.random.default_rng(2)
        yy, xx = np.indices((14, 16))
        fold = (xx // 4 % 2) + 2 * (yy // 4 % 2)
        v = np.ones(fold.shape)
        features = np.stack([xx / 16, yy / 14], axis=-1)
        plan = CatalogVariancePlan(features, fold, v, neighbors=8, exclusion_radius=3.0)
        t = np.linspace(-1, 1, 19)
        bases = np.repeat(np.c_[np.ones(19), t][None], 4, axis=0)
        residual = rng.normal(size=(19, *fold.shape)) + 10 * t[:, None, None]
        before = plan.predict(residual, np.ones(19), bases)
        selected = (xx - 7) ** 2 + (yy - 6) ** 2 <= 3**2
        residual[:, selected] += 100 * t[:, None]
        after = plan.predict(residual, np.ones(19), bases)
        np.testing.assert_array_equal(before[6, 7], after[6, 7])
        self.assertGreater(before[6, 7, 0], 0)

    def test_catalog_features_are_finite_with_zero_weight_and_outside_stars(self):
        v = np.ones((7, 9))
        v[0] = 0
        f = catalog_features(
            np.array([[3.2, 4.1], [-2.0, 2.0], [100.0, 100.0]]), np.array([20.0, 5.0, 100.0]), v
        )
        self.assertEqual(f.shape, (7, 9, 5))
        self.assertTrue(np.isfinite(f).all())


if __name__ == "__main__":
    unittest.main()
