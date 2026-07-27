"""Scoring rules for interval predictions.

Pinball loss is the objective a quantile model minimises, and coverage is
the honesty check on top of it: an interval that claims 80 percent and
holds 55 percent is not an uncertainty estimate, it is decoration.
"""

import unittest

from punktlig.quantiles import coverage, pinball


class PinballTest(unittest.TestCase):
    def test_symmetric_at_the_median(self):
        # At alpha 0.5 the loss is half the absolute error, either side.
        self.assertAlmostEqual(pinball([10.0], [0.0], 0.5), 5.0)
        self.assertAlmostEqual(pinball([0.0], [10.0], 0.5), 5.0)

    def test_high_quantile_punishes_under_prediction(self):
        # At alpha 0.9 predicting too low costs nine times as much as
        # predicting too high, which is what pushes the fit upwards.
        self.assertAlmostEqual(pinball([10.0], [0.0], 0.9), 9.0)
        self.assertAlmostEqual(pinball([0.0], [10.0], 0.9), 1.0)

    def test_zero_when_exact(self):
        self.assertAlmostEqual(pinball([3.0, -2.0], [3.0, -2.0], 0.5), 0.0)


class CoverageTest(unittest.TestCase):
    def test_counts_only_values_inside_the_interval(self):
        y = [0.0, 5.0, 10.0, 15.0]
        lo = [1.0, 1.0, 1.0, 1.0]
        hi = [11.0, 11.0, 11.0, 11.0]
        # 5 and 10 are inside, 0 and 15 are outside.
        self.assertAlmostEqual(coverage(y, lo, hi), 0.5)

    def test_bounds_are_inclusive(self):
        self.assertAlmostEqual(coverage([1.0, 2.0], [1.0, 1.0], [2.0, 2.0]), 1.0)

    def test_empty_is_none_not_a_crash(self):
        self.assertIsNone(coverage([], [], []))


if __name__ == "__main__":
    unittest.main()
