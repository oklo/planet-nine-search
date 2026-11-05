import numpy as np
import pytest
import torch

from p9search.science.composed_scoring import ComposedTemporalScorer
from p9search.science.sparse_composed import SparseComposedScorer

from .test_composed_scoring import ComposedScoringTests


def test_sparse_matches_dense_for_local_full_and_unidentifiable_sources():
    *_, template, region, scene, temporal = ComposedScoringTests().fixture()
    dense = ComposedTemporalScorer(scene, temporal, device="cpu")
    sparse = SparseComposedScorer(dense)
    rng = np.random.default_rng(8103)
    cases = [(template, region), (np.zeros(scene.shape), None), (np.ones(scene.shape), None)]
    cases += [(rng.normal(size=template.shape), region) for _ in range(20)]
    for value, where in cases:
        expected, actual = dense.score(value, where), sparse.score(value, where)
        assert expected["identifiable"] == actual["identifiable"]
        for key in ("numerator", "information", "flux", "sigma"):
            if expected[key] is not None:
                np.testing.assert_allclose(actual[key], expected[key], rtol=1e-7, atol=1e-9)
    assert sparse.fallbacks >= 2


def test_sparse_refuses_rounded_coefficients_nonseparable_weights_and_soft_basis():
    *_, scene, temporal = ComposedScoringTests().fixture()
    with pytest.raises(ValueError, match="float64"):
        SparseComposedScorer(ComposedTemporalScorer(scene, temporal, dtype=torch.float32))
    dense = ComposedTemporalScorer(scene, temporal)
    dense.spatial_basis *= 0.9
    with pytest.raises(ValueError, match="orthonormal"):
        SparseComposedScorer(dense)
    dense = ComposedTemporalScorer(scene, temporal)
    dense.weight[1, 2] *= 2
    with pytest.raises(ValueError, match="separable"):
        SparseComposedScorer(dense)
