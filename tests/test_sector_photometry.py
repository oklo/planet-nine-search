import unittest

import numpy as np

from p9search.science.sector_photometry import combine, coverage


class PartialPhotometryTests(unittest.TestCase):
    def test_partial_nondetection_penalizes_false_three_sector_peak(self):
        anchors = [dict(sector=i, flux=2.0, sigma=0.15, anchor=True) for i in range(3)]
        extra = [dict(sector=i + 3, flux=0.0, sigma=0.08, anchor=False) for i in range(3)]
        before, after = combine(anchors), combine(anchors + extra)
        self.assertGreater(before["coherent_score"], 20)
        self.assertLess(after["coherent_score"], 0)
        # Independent whitened least-squares oracle, including the residual sum.
        rows = anchors + extra
        errors = np.array([r["sigma"] for r in rows])
        design = np.ones((6, 1)) / errors[:, None]
        data = np.array([r["flux"] for r in rows]) / errors
        beta = np.linalg.lstsq(design, data, rcond=None)[0]
        np.testing.assert_allclose(after["flux"], beta[0], rtol=1e-13)
        np.testing.assert_allclose(
            after["sector_consistency_chi2"], np.sum((data - design @ beta) ** 2), rtol=1e-13
        )

    def test_partial_support_improves_constant_flux_and_sign_symmetry(self):
        rows = [dict(sector=i, flux=-2.0, sigma=1.0 / (i + 1), anchor=i < 3) for i in range(6)]
        positive = combine(rows)
        negative = combine(rows, sign=-1.0)
        self.assertAlmostEqual(negative["flux"], -2)
        self.assertAlmostEqual(negative["coherent_score"], 2 * np.sqrt(91))
        self.assertAlmostEqual(negative["formal_snr"], -positive["formal_snr"])
        self.assertEqual(negative["anchors"], 3)
        rows[2]["anchor"] = False
        self.assertIsNone(combine(rows))

    def test_geometry_counts_good_stamp_support_outside_center_margin(self):
        xy = np.array([[50, 50], [-4, 50], [145, 50], [146, 50], [20, 20]])
        result = coverage(xy, [True, True, True, True, False])
        self.assertEqual(result["coverage"], 0.25)
        self.assertEqual(result["stamp_overlap_fraction"], 0.75)

    def test_bad_or_duplicate_measurement_is_rejected(self):
        rows = [dict(sector=i, flux=1.0, sigma=1.0, anchor=True) for i in range(3)]
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            combine(rows + [rows[0]])
        rows[0]["sigma"] = float("nan")
        with self.assertRaisesRegex(ValueError, "Nonfinite"):
            combine(rows)


if __name__ == "__main__":
    unittest.main()
