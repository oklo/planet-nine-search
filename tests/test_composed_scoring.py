import unittest

import numpy as np

from p9search.science.adaptive_covariance import AdaptiveTemporalProjector
from p9search.science.composed_scoring import ComposedTemporalScorer
from p9search.science.scene_likelihood import SceneProjector


class ComposedScoringTests(unittest.TestCase):
    def fixture(self):
        rng = np.random.default_rng(62)
        shape = (17, 4, 5)
        y = rng.normal(size=shape)
        u = rng.uniform(0.5, 2, shape[0])
        u[0] = 0
        v = rng.uniform(0.5, 2, shape[1:])
        design = rng.normal(size=(20, 3))
        basis = np.c_[np.ones(17), np.linspace(-1, 1, 17), rng.normal(size=17)]
        bases = np.repeat(basis[None], 4, axis=0)
        fold = np.arange(20).reshape(4, 5) % 4
        lam = rng.uniform(0.1, 3, (4, 5, 2))
        lam[0] = 0
        p = rng.uniform(0, 1, (17, 2, 2))
        region = (slice(None), slice(1, 3), slice(2, 4))
        scene = SceneProjector(y, u, v, design)
        temporal = AdaptiveTemporalProjector(
            scene.residual, u[:, None, None] * v, bases, fold, prior_variances=lam
        )
        return y, u, v, design, bases, fold, lam, p, region, scene, temporal

    def test_matches_explicit_composed_source(self):
        *_, p, region, scene, temporal = self.fixture()
        full = np.zeros(scene.shape)
        full[region] = p
        matrix = full.reshape(len(full), -1)
        u = scene.time_weight
        v = scene.pixel_weight.ravel()
        time_operator = np.eye(len(full)) - np.ones((len(full), 1)) * u[None] / u.sum()
        spatial = np.eye(len(v)) - v[:, None] * scene.basis @ scene.basis.T
        propagated = (time_operator @ matrix @ spatial).reshape(full.shape)
        expected = temporal.score(propagated)
        actual = ComposedTemporalScorer(scene, temporal).score(p, region)
        for key in ["numerator", "information", "flux", "sigma"]:
            np.testing.assert_allclose(actual[key], expected[key], rtol=2e-12, atol=2e-12)

    def test_fixed_linear_operators_preserve_injected_flux(self):
        y, u, v, design, bases, fold, lam, p, region, scene, temporal = self.fixture()
        full = np.zeros(scene.shape)
        full[region] = p
        flux = 0.7
        after = SceneProjector(y + flux * full, u, v, design)
        noise = AdaptiveTemporalProjector(
            after.residual, u[:, None, None] * v, bases, fold, prior_variances=lam
        )
        before_score = ComposedTemporalScorer(scene, temporal).score(p, region)
        after_score = ComposedTemporalScorer(after, noise).score(p, region)
        self.assertAlmostEqual((after_score["flux"] - before_score["flux"]) / flux, 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
