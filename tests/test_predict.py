"""Predicting for vehicles that are running right now.

Training replays history and needs ground truth from a later snapshot.
Prediction asks the same question about the newest snapshot, where no
later one exists yet. Both must build features from the same code, or the
model is served something subtly different from what it was trained on.
"""

import tempfile
import unittest
from pathlib import Path

from punktlig import db
from punktlig.predict import upcoming_rows
from test_dataset import D, seed_archive

NEWEST = f"{D}T10:10:00+00:00"


def seed_vehicle_en_route(path):
    """A newest poll where a vehicle has passed one stop and has one to go.

    The replay scenario ends with every stop recorded, which is exactly the
    case with nothing left to predict.
    """
    conn = db.connect(path)
    base = dict(
        recorded_at=f"{D}T12:20:00+02:00", line_ref="RUT:Line:12", direction="1",
        journey_ref="RUT:ServiceJourney:live", operating_date=D,
        operator_ref="RUT:Operator:220", monitored=1, cancelled=0, call_cancelled=0,
    )
    passed = dict(base, call_type="recorded", stop_ref="NSR:Quay:1", stop_name="A",
                  order_no=1, aimed_dep=f"{D}T12:20:00+02:00",
                  actual_dep=f"{D}T12:21:00+02:00")
    ahead = dict(base, call_type="estimated", stop_ref="NSR:Quay:2", stop_name="B",
                 order_no=2, aimed_arr=f"{D}T12:25:00+02:00",
                 expected_arr=f"{D}T12:26:00+02:00")
    poll_id = db.insert_poll(conn, polled_at=NEWEST, feed="et", dataset="RUT", n_calls=2)
    db.insert_calls(conn, [dict(c, poll_id=poll_id) for c in (passed, ahead)])
    conn.close()


class UpcomingRowsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.archive = Path(cls.tmp.name) / "archive.db"
        seed_archive(cls.archive)
        seed_vehicle_en_route(cls.archive)
        cls.rows = upcoming_rows(
            archive_path=cls.archive, parquet_dir=Path(cls.tmp.name) / "none"
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_it_predicts_from_the_newest_sighting_of_each_journey(self):
        # The feed is polled with a requestorId, so a poll carries only the
        # journeys that changed. Reading the newest poll alone left most
        # vehicles off the map; recent polls are stitched together instead,
        # and the newest sighting of a journey is the one that counts.
        self.assertTrue(self.rows)
        live = [r for r in self.rows
                if r["journey_ref"] == "RUT:ServiceJourney:live"]
        self.assertTrue(live)
        self.assertEqual({r["polled_at"] for r in live}, {NEWEST})

    def test_journeys_missing_from_the_newest_poll_are_still_included(self):
        # The seeded replay journeys appear in earlier polls only. Losing
        # them is exactly the bug: a map with thirty vehicles instead of
        # four hundred.
        others = {r["journey_ref"] for r in self.rows} - {"RUT:ServiceJourney:live"}
        self.assertTrue(others)

    def test_rows_have_no_label_but_do_have_features(self):
        row = self.rows[0]
        self.assertIsNone(row["label_delay_sec"])
        self.assertIsNone(row["actual_ts"])
        self.assertIsNotNone(row["current_delay_sec"])
        self.assertIsNotNone(row["horizon_sec"])

    def test_entur_prediction_is_carried_for_comparison(self):
        # The point of the output is model against the official estimate,
        # so the official number has to travel with the row.
        self.assertTrue(all(r["entur_pred_delay_sec"] is not None for r in self.rows))

    def test_stops_already_passed_are_not_predicted(self):
        for row in self.rows:
            self.assertGreater(row["order_no"], row["current_order"])


if __name__ == "__main__":
    unittest.main()
