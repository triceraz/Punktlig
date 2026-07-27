"""Turning a ladder of quantiles into answers about a specific deadline.

The model fits a handful of quantiles of the delay. A passenger does not
ask for the 80th percentile, they ask "will it be here within two
minutes". That is the same distribution read the other way round, and this
pins the reading.
"""

import unittest

from punktlig.quantiles import LEVELS, enforce_monotonic, probability_within


class MonotonicTest(unittest.TestCase):
    def test_crossed_quantiles_are_repaired(self):
        # Quantiles fitted independently can cross. A 90th percentile below
        # the 50th is not a distribution, so the ladder is sorted per row.
        fixed = enforce_monotonic([[10.0, 5.0, 30.0], [1.0, 2.0, 3.0]])
        self.assertEqual(fixed[0], [5.0, 10.0, 30.0])
        self.assertEqual(fixed[1], [1.0, 2.0, 3.0])


class ProbabilityTest(unittest.TestCase):
    def setUp(self):
        # A ladder where the delay quantiles are conveniently readable.
        self.levels = (0.1, 0.5, 0.9)
        self.values = [60.0, 120.0, 300.0]  # p10 60s, p50 120s, p90 300s

    def test_threshold_at_a_known_quantile_returns_that_level(self):
        self.assertAlmostEqual(
            probability_within(self.values, 120.0, self.levels), 0.5
        )

    def test_interpolates_between_quantiles(self):
        # Halfway between p10 (60s) and p50 (120s) in delay terms.
        self.assertAlmostEqual(
            probability_within(self.values, 90.0, self.levels), 0.3
        )

    def test_below_the_lowest_quantile_is_capped_not_zero(self):
        p = probability_within(self.values, 0.0, self.levels)
        self.assertGreaterEqual(p, 0.0)
        self.assertLessEqual(p, 0.1)

    def test_above_the_highest_quantile_approaches_one(self):
        p = probability_within(self.values, 3600.0, self.levels)
        self.assertGreaterEqual(p, 0.9)
        self.assertLessEqual(p, 1.0)

    def test_probability_never_decreases_with_a_later_deadline(self):
        previous = 0.0
        for threshold in range(0, 600, 30):
            p = probability_within(self.values, float(threshold), self.levels)
            self.assertGreaterEqual(p, previous)
            previous = p


class LadderTest(unittest.TestCase):
    def test_levels_are_sorted_and_include_the_median(self):
        self.assertEqual(list(LEVELS), sorted(LEVELS))
        self.assertIn(0.5, LEVELS)


if __name__ == "__main__":
    unittest.main()
