import unittest
from pathlib import Path

from punktlig.siri import parse_et

FIXTURE = Path(__file__).parent / "fixtures" / "sample_et.xml"


class ParseEtTest(unittest.TestCase):
    def setUp(self):
        self.journeys, self.more = parse_et(FIXTURE.read_bytes())

    def test_more_data_flag(self):
        self.assertFalse(self.more)

    def test_journey_count_and_identity(self):
        self.assertEqual(len(self.journeys), 2)
        first = self.journeys[0]
        self.assertEqual(first["line_ref"], "RUT:Line:12")
        self.assertEqual(first["journey_ref"], "RUT:ServiceJourney:abc123")
        self.assertEqual(first["operating_date"], "2026-07-25")
        self.assertEqual(first["monitored"], 1)
        self.assertEqual(first["cancelled"], 0)

    def test_recorded_call_carries_actual_time(self):
        recorded = [c for c in self.journeys[0]["calls"] if c["call_type"] == "recorded"]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["stop_name"], "Majorstuen")
        self.assertEqual(recorded[0]["actual_dep"], "2026-07-25T10:44:30+02:00")
        self.assertIsNone(recorded[0]["expected_dep"])

    def test_estimated_calls_carry_predictions(self):
        estimated = [c for c in self.journeys[0]["calls"] if c["call_type"] == "estimated"]
        self.assertEqual(len(estimated), 2)
        self.assertEqual(estimated[0]["aimed_arr"], "2026-07-25T10:56:00+02:00")
        self.assertEqual(estimated[0]["expected_arr"], "2026-07-25T10:58:00+02:00")
        self.assertEqual(estimated[0]["order_no"], 2)
        self.assertEqual(estimated[1]["call_cancelled"], 1)

    def test_extra_journey_falls_back_to_journey_code(self):
        extra = self.journeys[1]
        self.assertEqual(extra["journey_ref"], "RUT:ServiceJourney:extra9")
        self.assertEqual(extra["cancelled"], 1)
        self.assertEqual(extra["monitored"], 0)


if __name__ == "__main__":
    unittest.main()
