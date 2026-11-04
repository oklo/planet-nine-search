import numpy as np

from p9search.science.catalog_covariance import CatalogVariancePlan
from p9search.science.continuous_catalog_covariance import ContinuousCatalogVariancePlan


def test_variance_turns_on_continuously_at_the_same_donor_power_threshold():
    h = w = 8
    t = np.arange(40)
    mode = np.sin(2 * np.pi * t / len(t))
    mode /= np.linalg.norm(mode)
    basis = np.tile(np.column_stack([np.ones(len(t)), mode])[None], (4, 1, 1))
    yy, xx = np.indices((h, w))
    fold = (yy % 2) * 2 + xx % 2
    features = np.zeros((h, w, 2))
    v = np.ones((h, w))
    u = np.ones(len(t))
    arguments = (features, fold, v)
    hard = CatalogVariancePlan(*arguments, neighbors=4, exclusion_radius=0.5)
    smooth = ContinuousCatalogVariancePlan(*arguments, neighbors=4, exclusion_radius=0.5)
    np.testing.assert_array_equal(hard.donors, smooth.donors)
    answers = []
    for power in [25 - 1e-4, 25 + 1e-4, 100.0]:
        residual = np.sqrt(power) * mode[:, None, None] * np.ones((1, h, w))
        old = hard.predict(residual, u, basis)
        new = smooth.predict(residual, u, basis)
        np.testing.assert_allclose(new, max(power - 25, 0), atol=1e-10)
        assert np.all(new >= 0) and np.isfinite(new).all()
        answers.append((old, new))
    assert np.min(answers[1][0]) > 24
    assert np.max(answers[1][1] - answers[0][1]) < 1.1e-4
