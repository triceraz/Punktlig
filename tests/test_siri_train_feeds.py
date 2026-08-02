"""The train operators identify journeys differently from the bus operators.

Ruter and Flytoget wrap the journey reference in FramedVehicleJourneyRef and
publish an operating day alongside it. Vy and Go-Ahead put a bare
DatedVehicleJourneyRef straight on the journey and publish no operating day.
Only the wrapped form was parsed, so every train arrived with journey_ref
empty, and the replay drops rows without one: the trains were collected and
then silently discarded, for months.
"""

import unittest

from punktlig.siri import parse_et

FRAMED = """<?xml version="1.0"?>
<Siri xmlns="http://www.siri.org.uk/siri">
  <EstimatedVehicleJourney>
    <LineRef>RUT:Line:12</LineRef>
    <FramedVehicleJourneyRef>
      <DataFrameRef>2026-08-02</DataFrameRef>
      <DatedVehicleJourneyRef>RUT:ServiceJourney:12-abc</DatedVehicleJourneyRef>
    </FramedVehicleJourneyRef>
    <EstimatedCalls>
      <EstimatedCall>
        <StopPointRef>NSR:Quay:1</StopPointRef>
        <Order>1</Order>
        <AimedDepartureTime>2026-08-02T06:11:00+02:00</AimedDepartureTime>
      </EstimatedCall>
    </EstimatedCalls>
  </EstimatedVehicleJourney>
</Siri>"""

BARE = """<?xml version="1.0"?>
<Siri xmlns="http://www.siri.org.uk/siri">
  <EstimatedVehicleJourney>
    <LineRef>VYG:Line:RE20</LineRef>
    <DatedVehicleJourneyRef>VYG:DatedServiceJourney:383-103_26-08-05</DatedVehicleJourneyRef>
    <VehicleMode>rail</VehicleMode>
    <EstimatedCalls>
      <EstimatedCall>
        <StopPointRef>NSR:Quay:567</StopPointRef>
        <Order>1</Order>
        <AimedDepartureTime>2026-08-05T06:11:00+02:00</AimedDepartureTime>
      </EstimatedCall>
    </EstimatedCalls>
  </EstimatedVehicleJourney>
</Siri>"""

CODE_ONLY = """<?xml version="1.0"?>
<Siri xmlns="http://www.siri.org.uk/siri">
  <EstimatedVehicleJourney>
    <LineRef>XXX:Line:1</LineRef>
    <EstimatedVehicleJourneyCode>XXX:Code:9</EstimatedVehicleJourneyCode>
    <EstimatedCalls/>
  </EstimatedVehicleJourney>
</Siri>"""


class JourneyIdentityTest(unittest.TestCase):
    def test_the_wrapped_form_still_carries_both(self):
        (journey,), _ = parse_et(FRAMED)
        self.assertEqual(journey["journey_ref"], "RUT:ServiceJourney:12-abc")
        self.assertEqual(journey["operating_date"], "2026-08-02")

    def test_the_bare_form_is_read_as_the_journey_reference(self):
        (journey,), _ = parse_et(BARE)
        self.assertEqual(journey["journey_ref"],
                         "VYG:DatedServiceJourney:383-103_26-08-05")

    def test_a_feed_without_an_operating_day_leaves_it_empty(self):
        (journey,), _ = parse_et(BARE)
        self.assertIsNone(journey["operating_date"])

    def test_the_journey_code_remains_the_last_resort(self):
        (journey,), _ = parse_et(CODE_ONLY)
        self.assertEqual(journey["journey_ref"], "XXX:Code:9")

    def test_the_wrapped_reference_wins_over_a_bare_one(self):
        both = FRAMED.replace(
            "</FramedVehicleJourneyRef>",
            "</FramedVehicleJourneyRef><DatedVehicleJourneyRef>WRONG</DatedVehicleJourneyRef>",
        )
        (journey,), _ = parse_et(both)
        self.assertEqual(journey["journey_ref"], "RUT:ServiceJourney:12-abc")


if __name__ == "__main__":
    unittest.main()
