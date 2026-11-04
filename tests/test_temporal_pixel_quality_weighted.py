import numpy as np

from p9search.science.temporal_pixel_quality_weighted import weighted_persistent_steps

from .reference import persistent_steps


def test_equal_time_weights_reproduce_original_abrupt_quality_exactly():
    rng = np.random.default_rng(37)
    data = rng.normal(0, 0.1, (120, 5, 6))
    data[53:, 2, 3] += 2.0
    data[40:42, 1, 1] += 5.0
    t = np.arange(120) / 48
    keep = np.ones(120, bool)
    sigma = np.full((5, 6), 0.1)
    original = persistent_steps(
        data, t, keep, sigma, minimum_instant_fraction=0.5, minimum_instant_score=8.0
    )
    weighted = weighted_persistent_steps(data, t, keep, sigma, np.full(120, 7.0))
    np.testing.assert_array_equal(original["mask"], weighted["mask"])
    np.testing.assert_allclose(
        original["maximum_step_score"], weighted["maximum_step_score"], atol=1e-12
    )


def test_already_downweighted_exposure_event_does_not_mask_every_pixel():
    data = np.zeros((120, 5, 6))
    data[30:48] = 5.0
    data[75:, 2, 3] += 2.5
    u = np.ones(120)
    u[30:48] = 0.001
    t = np.arange(120) / 48
    keep = np.ones(120, bool)
    sigma = np.full((5, 6), 0.1)
    old = persistent_steps(
        data, t, keep, sigma, minimum_instant_fraction=0.5, minimum_instant_score=8.0
    )
    assert old["mask"].all()
    result = weighted_persistent_steps(data, t, keep, sigma, u)
    expected = np.zeros((5, 6), bool)
    expected[2, 3] = True
    np.testing.assert_array_equal(result["mask"], expected)


def test_zero_weight_samples_are_excluded_and_weight_units_do_not_change_result():
    data = np.zeros((120, 3, 4))
    data[55:, 1, 2] = 3.0
    data[25] = 1e5
    u = np.ones(120)
    u[25] = 0.0
    u[60:70] = 0.2
    t = np.arange(120) / 48
    keep = np.ones(120, bool)
    sigma = np.full((3, 4), 0.1)
    a = weighted_persistent_steps(data, t, keep, sigma, u)
    b = weighted_persistent_steps(data, t, keep, sigma, u * 5)
    expected = np.zeros((3, 4), bool)
    expected[1, 2] = True
    np.testing.assert_array_equal(a["mask"], expected)
    np.testing.assert_allclose(a["maximum_step_score"], b["maximum_step_score"])
