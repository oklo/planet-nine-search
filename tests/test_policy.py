import json
from pathlib import Path

import numpy as np
import pytest

from p9search.calibration import has_source, response_admitted, unchanged_pixels
from p9search.policy import default_policy, screen_rows, validate_policy
from p9search.science.physical_prior import screen


def test_reviewed_n104_retirements_and_n16_retained_flags():
    cases = json.loads((Path(__file__).parent / "fixtures/prior_dispositions.json").read_text())
    assert len(cases) == 6
    for case in cases:
        result = screen(
            case["parameters"], case["native"]["flux"], case["native"]["sigma"], case["context"]
        )
        assert result["eligible_for_p9_followup"] == case["expected_eligible"]
        if case["field"] == "N104":
            assert "brightness_requires_excessive_radius_or_albedo" in result["reasons"]
        rows = [{"parameters": case["parameters"], "native": case["native"]}]
        screen_rows(rows, case["context"])
        assert rows[0]["prior"] == result


def test_zero_support_is_exact_and_requires_bitwise_unchanged_native_pixels():
    empty = np.zeros((3, 2, 2))
    assert not has_source(empty)
    tiny = empty.copy()
    tiny[0, 0, 0] = np.nextafter(0.0, 1.0)
    assert has_source(tiny)
    with pytest.raises(ValueError, match="Nonfinite"):
        has_source(np.array([np.nan]))
    unchanged_pixels(empty, empty.copy())
    with pytest.raises(ValueError, match="changed"):
        unchanged_pixels(empty, tiny)
    signed_zero = empty.copy()
    signed_zero[0, 0, 0] = -0.0
    with pytest.raises(ValueError, match="changed"):
        unchanged_pixels(empty, signed_zero)
    informative = [{"has_source": True, "assessed": True}] * 3
    assert response_admitted([*informative, {"has_source": False, "assessed": True}])
    assert not response_admitted(informative[:2] + [{"has_source": False, "assessed": True}])
    assert not response_admitted([*informative, {"has_source": True, "assessed": False}])


def test_empirical_selection_is_bound_to_task_sectors():
    policy = default_policy()
    policy["empirical_star_sectors"] = [68]
    validate_policy(policy, [1, 28, 68, 95])
    with pytest.raises(ValueError):
        validate_policy(policy, [1, 28, 95])


def test_reference_propagation_matches_circular_two_body_orbit():
    from p9search.science.prior_population import elements_state, propagate_batch

    p, v = elements_state(np.array([500.0]), 0.0, 10.0, 25.0, 15.0, 5.0)
    expected_p, expected_v = elements_state(
        np.array([500.0]), 0.0, 10.0, 25.0, 15.0, 5.0, days=600.0
    )
    actual_p, actual_v = propagate_batch(p, v, [600.0])
    np.testing.assert_allclose(actual_p[0], expected_p, atol=1e-8, rtol=0)
    np.testing.assert_allclose(actual_v[0], expected_v, atol=1e-12, rtol=0)
