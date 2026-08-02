"""Recovering trains from days that had already been compacted.

The parser used to drop the train operators' journey references, so those
rows were archived without identity and every consumer filtered them away.
Repairing a compacted day means rewriting its parquet: the rows that are
still usable, plus the ones parsed again from the raw responses.
"""

import gzip
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import duckdb
    HAVE_DUCKDB = True
except ImportError:
    HAVE_DUCKDB = False

from punktlig import recover

DAY = "2026-07-28"

BARE_TRAIN = """<?xml version="1.0"?>
<Siri xmlns="http://www.siri.org.uk/siri">
  <EstimatedVehicleJourney>
    <LineRef>VYG:Line:RE20</LineRef>
    <DatedVehicleJourneyRef>VYG:DatedServiceJourney:1</DatedVehicleJourneyRef>
    <Monitored>true</Monitored>
    <Cancellation>false</Cancellation>
    <EstimatedCalls>
      <EstimatedCall>
        <StopPointRef>NSR:Quay:567</StopPointRef>
        <Order>1</Order>
        <StopPointName>Oslo S</StopPointName>
        <AimedDepartureTime>2026-07-28T06:11:00+02:00</AimedDepartureTime>
        <ExpectedDepartureTime>2026-07-28T06:13:00+02:00</ExpectedDepartureTime>
      </EstimatedCall>
      <EstimatedCall>
        <StopPointRef>NSR:Quay:201</StopPointRef>
        <Order>2</Order>
        <StopPointName>Tangen i Sannidal (Bø, Lunde, Drangedal)</StopPointName>
        <AimedArrivalTime>2026-07-28T06:22:00+02:00</AimedArrivalTime>
      </EstimatedCall>
    </EstimatedCalls>
  </EstimatedVehicleJourney>
</Siri>"""


@unittest.skipUnless(HAVE_DUCKDB, "duckdb not installed (analysis extra)")
class RecoverCompactedDayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.parquet = self.root / "parquet"
        (self.parquet / "calls").mkdir(parents=True)
        (self.parquet / "polls").mkdir(parents=True)
        raw = self.root / "raw" / "et" / DAY
        raw.mkdir(parents=True)
        (raw / "061500_VYG_p1.xml.gz").write_bytes(gzip.compress(BARE_TRAIN.encode()))

        self.duck = duckdb.connect()
        self.addCleanup(self.duck.close)
        self.polled_at = f"{DAY}T06:15:00+00:00"
        # DuckDB will not bind the destination of a COPY, so paths are written
        # into the statement; forward slashes keep Windows out of the way.
        polls_path = (self.parquet / "polls" / f"{DAY}.parquet").as_posix()
        self.duck.execute(
            "CREATE TABLE seed_polls AS SELECT * FROM (VALUES "
            f"  (6::BIGINT, '{self.polled_at}', 'et', 'RUT', 1::BIGINT, "
            "   1::BIGINT, 1::BIGINT, 0::BIGINT, 5::BIGINT, NULL::VARCHAR), "
            f"  (7::BIGINT, '{self.polled_at}', 'et', 'VYG', 1::BIGINT, "
            "   1::BIGINT, 2::BIGINT, 0::BIGINT, 5::BIGINT, NULL::VARCHAR)"
            ") AS t(poll_id, polled_at, feed, dataset, pages, n_journeys, "
            "n_calls, n_dropped, duration_ms, error)"
        )
        self.duck.execute(f"COPY seed_polls TO '{polls_path}' (FORMAT PARQUET)")

        # One usable tram row in its own poll, and two train rows archived
        # without identity in the train poll.
        self.duck.execute(
            "CREATE TABLE seed AS SELECT * FROM (VALUES "
            f"  ('RUT:Journey:1', '{DAY}', 6::BIGINT, '{self.polled_at}', "
            "   'RUT:Line:12', '1', 'recorded', 'NSR:Quay:9', 'Majorstuen', "
            "   1::BIGINT, NULL, NULL, NULL, NULL, NULL, NULL, 0::BIGINT, "
            "   0::BIGINT, NULL, NULL, 1::BIGINT), "
            f"  (NULL, NULL, 7::BIGINT, '{self.polled_at}', 'VYG:Line:RE20', NULL, "
            "   'estimated', 'NSR:Quay:567', 'Oslo S', 1::BIGINT, NULL, NULL, NULL, "
            "   NULL, NULL, NULL, 0::BIGINT, 0::BIGINT, NULL, NULL, 1::BIGINT), "
            f"  (NULL, NULL, 7::BIGINT, '{self.polled_at}', 'VYG:Line:RE20', NULL, "
            "   'estimated', 'NSR:Quay:201', 'gammelt navn', 2::BIGINT, NULL, NULL, NULL, "
            "   NULL, NULL, NULL, 0::BIGINT, 0::BIGINT, NULL, NULL, 1::BIGINT)"
            f") AS t({', '.join(recover.CALL_NAMES)})"
        )
        self.calls = self.parquet / "calls" / f"{DAY}.parquet"
        self.duck.execute(
            f"COPY seed TO '{self.calls.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )

        self.modes = {"RUT:Line:12": "tram", "VYG:Line:RE20": "rail"}

    def rows_now(self):
        return self.duck.execute(
            "SELECT journey_ref, line_ref, stop_name, order_no, polled_at, poll_id "
            "FROM read_parquet(?) ORDER BY line_ref, order_no", [str(self.calls)]
        ).fetchall()

    def parsed(self):
        index = recover.poll_index(self.duck, self.parquet, DAY)
        return list(recover.recovered_calls(
            DAY, self.root / "raw", self.modes, index,
            sources=("VYG",), allowed=("rail",)
        ))

    def run_recovery(self):
        index = recover.poll_index(self.duck, self.parquet, DAY)
        rows = recover.recovered_calls(
            DAY, self.root / "raw", self.modes, index,
            sources=("VYG",), allowed=("rail",)
        )
        replaced = {poll_id for _, poll_id, _ in index.get("VYG", [])}
        return recover.rewrite_day(self.duck, self.parquet, DAY, rows, replaced)

    def test_the_poll_index_reads_the_compacted_polls(self):
        index = recover.poll_index(self.duck, self.parquet, DAY)
        self.assertIn("VYG", index)
        self.assertEqual(index["VYG"][0][1], 7)

    def test_a_response_far_from_any_poll_is_not_attached(self):
        index = recover.poll_index(self.duck, self.parquet, DAY)
        far = datetime.fromisoformat(self.polled_at) + timedelta(minutes=30)
        self.assertIsNone(recover.match_poll(index, "VYG", far))

    def test_recovered_rows_carry_identity_and_their_poll(self):
        rows = self.parsed()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["journey_ref"], "VYG:DatedServiceJourney:1")
            self.assertEqual(row["poll_id"], 7)
            self.assertEqual(row["polled_at"], self.polled_at)

    def test_the_rewritten_day_keeps_usable_rows_and_replaces_the_rest(self):
        stats = self.run_recovery()
        self.assertEqual(stats["before"], 3)
        self.assertEqual(stats["dropped"], 2)
        self.assertEqual(stats["added"], 2)
        self.assertEqual(stats["after"], 3)

    def test_no_row_is_left_without_identity(self):
        self.run_recovery()
        self.assertTrue(all(row[0] for row in self.rows_now()))

    def test_a_stop_name_containing_commas_survives_the_round_trip(self):
        # Real names do this: "Tangen i Sannidal (Bø, Lunde, Drangedal)".
        # Staged through CSV, a guessed dialect split it into extra columns.
        self.run_recovery()
        names = [row[2] for row in self.rows_now()]
        self.assertIn("Tangen i Sannidal (Bø, Lunde, Drangedal)", names)

    def test_the_bus_row_survives_untouched(self):
        self.run_recovery()
        bus = [r for r in self.rows_now() if r[1] == "RUT:Line:12"]
        self.assertEqual(len(bus), 1)
        self.assertEqual(bus[0][0], "RUT:Journey:1")

    def test_the_original_is_kept_as_a_backup(self):
        self.run_recovery()
        self.assertTrue(Path(str(self.calls) + ".bak").exists())

    def test_the_schema_still_matches_the_compactor(self):
        self.run_recovery()
        got = [c[0] for c in self.duck.execute(
            "DESCRIBE SELECT * FROM read_parquet(?)", [str(self.calls)]).fetchall()]
        self.assertEqual(got, recover.CALL_NAMES)

    def test_running_it_twice_changes_nothing_further(self):
        self.run_recovery()
        first = self.rows_now()
        self.run_recovery()
        self.assertEqual(self.rows_now(), first)


if __name__ == "__main__":
    unittest.main()
