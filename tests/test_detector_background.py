import numpy as np

from p9search.science.detector_background import detector_background_columns
from p9search.science.scene_likelihood import SceneProjector


def test_background_columns_are_independent_and_span_column_curvature_and_row_slopes():
    h, w = 11, 13
    columns = detector_background_columns((h, w), order=2)
    assert columns.shape == (h * w, 3 * (h + w - 3))
    # SceneProjector forms its normal matrix in float64; avoid testing the
    # unrelated rounding error of a long float32 dot-product reduction.
    columns = columns.astype(float)
    np.testing.assert_allclose(columns.T @ columns, np.eye(columns.shape[1]), atol=1e-7)
    rng = np.random.default_rng(18)
    yy, xx = np.indices((h, w))
    x = xx / w
    y = yy / h
    image = np.zeros((h, w))
    for degree in range(3):
        image += rng.normal(size=w)[None] * y**degree
        image += rng.normal(size=h)[:, None] * x**degree
    reconstructed = columns @ (columns.T @ image.ravel())
    np.testing.assert_allclose(reconstructed.reshape(h, w), image, atol=1e-6)


def test_variable_stripes_and_illumination_removed_with_unbiased_moving_source():
    h, w = 19, 23
    yy, xx = np.indices((h, w))
    t = np.linspace(-1, 1, 61)
    stripe = (xx == 8) * (1 + 0.04 * yy + 0.002 * yy**2)
    illumination = (yy / h) ** 2 * (1 + 0.3 * xx / w)
    row = (yy == 12) * (1 + 0.01 * xx)
    background = (
        50 * np.sin(4 * t)[:, None, None] * stripe
        + 70 * np.cos(2 * t)[:, None, None] * illumination
        + 20 * t[:, None, None] ** 2 * row
    )
    source = np.array(
        [
            np.exp(-((xx - (6 + 5 * (v + 1))) ** 2 + (yy - (8 + 0.7 * v)) ** 2) / 1.7)
            / (1.7 * np.pi)
            for v in t
        ]
    )
    data = background + 0.37 * source + 100
    weights = 1 / (2 + np.maximum(background.mean(axis=0), 0))
    scene = SceneProjector(data, 1 + 0.3 * t, weights, detector_background_columns((h, w)))
    fitted = scene.score(source)
    assert fitted["identifiable"] and fitted["information_fraction"] > 0.1
    np.testing.assert_allclose(fitted["flux"], 0.37, atol=3e-6)
