"""Replay tests on a synthetic archive.

The scenario: one tram journey observed over three polls.

  poll 1 (10:00:30Z): passed stop 1 (120s late), stops 2 and 3 still ahead
  poll 2 (10:02:00Z): passed stop 2 (150s late),  stop 3 still ahead
  poll 3 (10:05:00Z): passed stop 3 (180s late)

Plus a cancelled journey and weather snapshots issued before and after poll 1,
to prove the as-of-T join never picks a forecast from the future.
"""

import tempfile
import unittest
from pathlib import Path

from punktlig import db
from punktlig.dataset import build

D = "2026-07-25"


def call(**kw):
    base = dict(
        recorded_at=f"{D}T12:00:00+02:00",
        line_ref="RUT:Line:12",
        direction="1",
        journey_ref="RUT:ServiceJourney:test1",
        operating_date=D,
        operator_ref="RUT:Operator:220",
        monitored=1,
        cancelled=0,
        call_cancelled=0,
    )
    base.update(kw)
    return base


def seed_archive(path):
    conn = db.connect(path)

    weather = [
        # issued 09:00Z for the 10Z hour, superseded before the polls start
        {"polled_at": f"{D}T09:00:00+00:00", "forecast_time": f"{D}T10:00:00Z", "air_temp": 10.0},
        # issued 09:59Z: the newest forecast available at poll 1 (10:00:30Z)
        {"polled_at": f"{D}T09:59:00+00:00", "forecast_time": f"{D}T10:00:00Z", "air_temp": 12.0},
        # issued 10:01Z: exists in the archive but is in poll 1's future
        {"polled_at": f"{D}T10:01:00+00:00", "forecast_time": f"{D}T10:00:00Z", "air_temp": 99.0},
    ]
    for w in weather:
        w.setdefault("lat", 59.9)
        w.setdefault("lon", 10.7)
    db.insert_weather(conn, weather)

    stop1_recorded = call(
        call_type="recorded", stop_ref="NSR:Quay:1", stop_name="A", order_no=1,
        aimed_dep=f"{D}T11:58:00+02:00", actual_dep=f"{D}T12:00:00+02:00",
    )
    stop2_estimated = call(
        call_type="estimated", stop_ref="NSR:Quay:2", stop_name="B", order_no=2,
        aimed_arr=f"{D}T12:03:00+02:00", expected_arr=f"{D}T12:05:00+02:00",
    )
    stop3_estimated = call(
        call_type="estimated", stop_ref="NSR:Quay:3", stop_name="C", order_no=3,
        aimed_arr=f"{D}T12:06:00+02:00", expected_arr=f"{D}T12:08:00+02:00",
    )
    stop2_recorded = call(
        call_type="recorded", stop_ref="NSR:Quay:2", stop_name="B", order_no=2,
        aimed_arr=f"{D}T12:03:00+02:00", actual_arr=f"{D}T12:05:30+02:00",
    )
    stop3_recorded = call(
        call_type="recorded", stop_ref="NSR:Quay:3", stop_name="C", order_no=3,
        aimed_arr=f"{D}T12:06:00+02:00", actual_arr=f"{D}T12:09:00+02:00",
    )
    cancelled_journey = call(
        journey_ref="RUT:ServiceJourney:dead", cancelled=1,
        call_type="estimated", stop_ref="NSR:Quay:9", stop_name="X", order_no=2,
        aimed_arr=f"{D}T12:10:00+02:00", expected_arr=f"{D}T12:10:00+02:00",
    )

    polls = [
        (f"{D}T10:00:30+00:00", [stop1_recorded, stop2_estimated, stop3_estimated, cancelled_journey]),
        (f"{D}T10:02:00+00:00", [stop1_recorded, stop2_recorded, stop3_estimated]),
        (f"{D}T10:05:00+00:00", [stop1_recorded, stop2_recorded, stop3_recorded]),
    ]
    for polled_at, calls in polls:
        poll_id = db.insert_poll(conn, polled_at=polled_at, feed="et", dataset="RUT")
        db.insert_calls(conn, [dict(c, poll_id=poll_id) for c in calls])
    conn.close()


class DatasetBuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        archive = Path(cls.tmp.name) / "archive.db"
        out = Path(cls.tmp.name) / "dataset.db"
        seed_archive(archive)
        cls.n_written = build(archive_path=archive, out_path=out)
        conn = db.connect(out)
        cols = [c[1] for c in conn.execute("PRAGMA table_info(training_row)")]
        cls.rows = [dict(zip(cols, r)) for r in conn.execute("SELECT * FROM training_row")]
        conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _row(self, poll_id, order_no):
        matches = [r for r in self.rows if r["poll_id"] == poll_id and r["order_no"] == order_no]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_exactly_three_rows(self):
        # poll 1 -> stops 2 and 3; poll 2 -> stop 3; poll 3 -> nothing ahead.
        self.assertEqual(self.n_written, 3)

    def test_cancelled_journey_produces_no_rows(self):
        self.assertFalse([r for r in self.rows if r["journey_ref"] == "RUT:ServiceJourney:dead"])

    def test_features_reflect_state_at_poll_time(self):
        row = self._row(1, 2)
        self.assertEqual(row["current_order"], 1)
        self.assertEqual(row["n_recorded"], 1)
        self.assertEqual(row["current_delay_sec"], 120.0)  # dep 12:00 vs aimed 11:58
        self.assertEqual(row["horizon_sec"], 270.0)  # expected 10:05:00Z - polled 10:00:30Z
        self.assertEqual(row["horizon_stops"], 1)

    def test_label_and_entur_prediction(self):
        row = self._row(1, 2)
        self.assertEqual(row["label_delay_sec"], 150.0)  # actual 12:05:30 vs aimed 12:03
        self.assertEqual(row["entur_pred_delay_sec"], 120.0)  # expected 12:05 vs aimed 12:03

    def test_current_state_advances_between_polls(self):
        row = self._row(2, 3)
        self.assertEqual(row["current_order"], 2)
        self.assertEqual(row["current_delay_sec"], 150.0)
        self.assertEqual(row["label_delay_sec"], 180.0)  # actual 12:09 vs aimed 12:06

    def test_weather_join_never_uses_future_forecasts(self):
        # At poll 1 (10:00:30Z) the newest forecast for the 10Z hour was issued
        # 09:59Z (12.0°). The 99.0° snapshot exists but was issued 10:01Z, in
        # poll 1's future, and must be invisible to poll 1's rows.
        self.assertEqual(self._row(1, 2)["fc_air_temp"], 12.0)
        # At poll 2 (10:02:00Z) the 10:01Z snapshot IS the newest known one.
        self.assertEqual(self._row(2, 3)["fc_air_temp"], 99.0)


if __name__ == "__main__":
    unittest.main()
