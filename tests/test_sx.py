"""Parsing SIRI-SX deviation messages into rows the model can use.

The fixture is a real response from the live feed. The parser has to survive
the shapes Entur actually publishes: situations with several affected lines,
situations affecting stops but no line, and validity periods with no end.
"""

import unittest
from pathlib import Path

from punktlig.siri import parse_sx

FIXTURE = Path(__file__).parent / "fixtures" / "sample_sx.xml"


class ParseSxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.situations = parse_sx(FIXTURE.read_bytes())

    def test_finds_every_situation(self):
        self.assertEqual(len(self.situations), 55)

    def test_situation_identity_and_validity(self):
        s = self.situations[0]
        self.assertEqual(s["situation_number"], "RUT:SituationNumber:822487")
        self.assertEqual(s["progress"], "open")
        self.assertEqual(s["severity"], "normal")
        self.assertEqual(s["report_type"], "general")
        self.assertEqual(s["start_time"], "2026-07-04T04:00:00+02:00")
        self.assertEqual(s["end_time"], "2026-08-30T23:59:00+02:00")

    def test_affected_lines_are_collected(self):
        self.assertEqual(self.situations[0]["line_refs"], ["RUT:Line:3701"])
        # Every parsed line reference looks like a line reference.
        for s in self.situations:
            for line_ref in s["line_refs"]:
                self.assertRegex(line_ref, r"^[A-Z]+:Line:")

    def test_some_situation_affects_several_lines(self):
        self.assertTrue(any(len(s["line_refs"]) > 1 for s in self.situations))

    def test_missing_end_time_is_none_not_an_error(self):
        # Open-ended situations are normal; they must parse, not raise.
        for s in self.situations:
            self.assertIn(type(s["end_time"]).__name__, ("str", "NoneType"))


if __name__ == "__main__":
    unittest.main()
