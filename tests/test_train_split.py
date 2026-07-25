import unittest

try:
    from punktlig.train import day_split
    HAVE_LIGHTGBM = True
except ImportError:
    HAVE_LIGHTGBM = False


@unittest.skipUnless(HAVE_LIGHTGBM, "lightgbm not installed (analysis extra)")
class DaySplitTest(unittest.TestCase):
    def rows(self, *dates):
        # Minimal stand-in rows: the split only looks at the last element.
        return [("x", d) for d in dates]

    def test_last_date_becomes_validation(self):
        rows = self.rows("d1", "d1", "d2", "d3", "d3")
        reordered, split, valid_dates = day_split(rows, valid_days=1)
        self.assertEqual(split, 3)
        self.assertEqual(valid_dates, ["d3"])
        self.assertTrue(all(r[-1] != "d3" for r in reordered[:split]))
        self.assertTrue(all(r[-1] == "d3" for r in reordered[split:]))

    def test_journeys_never_straddle_the_split(self):
        # Rows arrive interleaved by poll time; the split must still separate
        # them strictly by date.
        rows = self.rows("d2", "d1", "d2", "d1", "d2")
        reordered, split, _ = day_split(rows, valid_days=1)
        self.assertEqual({r[-1] for r in reordered[:split]}, {"d1"})
        self.assertEqual({r[-1] for r in reordered[split:]}, {"d2"})

    def test_too_few_dates_returns_none(self):
        self.assertIsNone(day_split(self.rows("d1", "d1"), valid_days=1))
        self.assertIsNone(day_split(self.rows("d1", "d2"), valid_days=2))


if __name__ == "__main__":
    unittest.main()
