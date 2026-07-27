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


def seed_archive(path, day=D):
    """Insert the three-poll scenario for the given operating day."""
    conn = db.connect(path)

    def call(**kw):
        base = dict(
            recorded_at=f"{day}T12:00:00+02:00",
            line_ref="RUT:Line:12",
            direction="1",
            journey_ref="RUT:ServiceJourney:test1",
            operating_date=day,
            operator_ref="RUT:Operator:220",
            monitored=1,
            cancelled=0,
            call_cancelled=0,
        )
        base.update(kw)
        return base

    weather = [
        # issued 09:00Z for the 10Z hour, superseded before the polls start
        {"polled_at": f"{day}T09:00:00+00:00", "forecast_time": f"{day}T10:00:00Z", "air_temp": 10.0},
        # issued 09:59Z: the newest forecast available at poll 1 (10:00:30Z)
        {"polled_at": f"{day}T09:59:00+00:00", "forecast_time": f"{day}T10:00:00Z", "air_temp": 12.0},
        # issued 10:01Z: exists in the archive but is in poll 1's future
        {"polled_at": f"{day}T10:01:00+00:00", "forecast_time": f"{day}T10:00:00Z", "air_temp": 99.0},
    ]
    for w in weather:
        w.setdefault("lat", 59.9)
        w.setdefault("lon", 10.7)
    db.insert_weather(conn, weather)

    # Deviation feed: one situation known before the polls start, one that
    # only appears in a snapshot taken after poll 1.
    db.insert_situations(conn, f"{day}T09:30:00+00:00", [{
        "situation_number": "RUT:SituationNumber:1", "line_refs": ["RUT:Line:12"],
        "progress": "open", "severity": "normal", "report_type": "incident",
        "start_time": f"{day}T06:00:00+00:00", "end_time": f"{day}T22:00:00+00:00",
    }])
    db.insert_situations(conn, f"{day}T10:03:00+00:00", [{
        "situation_number": "RUT:SituationNumber:2", "line_refs": ["RUT:Line:12"],
        "progress": "open", "severity": "severe", "report_type": "incident",
        "start_time": f"{day}T10:00:00+00:00", "end_time": None,
    }])

    stop1_recorded = call(
        call_type="recorded", stop_ref="NSR:Quay:1", stop_name="A", order_no=1,
        aimed_dep=f"{day}T11:58:00+02:00", actual_dep=f"{day}T12:00:00+02:00",
    )
    stop2_estimated = call(
        call_type="estimated", stop_ref="NSR:Quay:2", stop_name="B", order_no=2,
        aimed_arr=f"{day}T12:03:00+02:00", expected_arr=f"{day}T12:05:00+02:00",
    )
    stop3_estimated = call(
        call_type="estimated", stop_ref="NSR:Quay:3", stop_name="C", order_no=3,
        aimed_arr=f"{day}T12:06:00+02:00", expected_arr=f"{day}T12:08:00+02:00",
    )
    stop2_recorded = call(
        call_type="recorded", stop_ref="NSR:Quay:2", stop_name="B", order_no=2,
        aimed_arr=f"{day}T12:03:00+02:00", actual_arr=f"{day}T12:05:30+02:00",
    )
    stop3_recorded = call(
        call_type="recorded", stop_ref="NSR:Quay:3", stop_name="C", order_no=3,
        aimed_arr=f"{day}T12:06:00+02:00", actual_arr=f"{day}T12:09:00+02:00",
    )
    cancelled_journey = call(
        journey_ref="RUT:ServiceJourney:dead", cancelled=1,
        call_type="estimated", stop_ref="NSR:Quay:9", stop_name="X", order_no=2,
        aimed_arr=f"{day}T12:10:00+02:00", expected_arr=f"{day}T12:10:00+02:00",
    )

    polls = [
        (f"{day}T10:00:30+00:00", [stop1_recorded, stop2_estimated, stop3_estimated, cancelled_journey]),
        (f"{day}T10:02:00+00:00", [stop1_recorded, stop2_recorded, stop3_estimated]),
        (f"{day}T10:05:00+00:00", [stop1_recorded, stop2_recorded, stop3_recorded]),
    ]
    poll_ids = []
    for polled_at, calls in polls:
        poll_id = db.insert_poll(conn, polled_at=polled_at, feed="et", dataset="RUT")
        db.insert_calls(conn, [dict(c, poll_id=poll_id) for c in calls])
        poll_ids.append(poll_id)
    conn.close()
    return poll_ids


class DatasetBuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        archive = Path(cls.tmp.name) / "archive.db"
        out = Path(cls.tmp.name) / "dataset.db"
        seed_archive(archive)
        cls.n_written = build(
            archive_path=archive, out_path=out,
            parquet_dir=Path(cls.tmp.name) / "no-parquet", bucket_seconds=60,
        )
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

    def test_sched_runtime_from_aimed_times(self):
        # Scheduled remaining runtime: aimed(target) minus aimed(current stop).
        # Poll 1, current stop 1 (aimed_dep 11:58): stop 2 aimed 12:03 -> 300s,
        # stop 3 aimed 12:06 -> 480s. Poll 2, current stop 2 (aimed 12:03):
        # stop 3 -> 180s.
        self.assertEqual(self._row(1, 2)["sched_runtime_sec"], 300.0)
        self.assertEqual(self._row(1, 3)["sched_runtime_sec"], 480.0)
        self.assertEqual(self._row(2, 3)["sched_runtime_sec"], 180.0)

    def test_bunching_is_null_without_prior_passes(self):
        # No other vehicle has passed any target stop at any row's T, so both
        # bunching features must be NULL for the whole single-journey scenario.
        for poll_id, order_no in ((1, 2), (1, 3), (2, 3)):
            self.assertIsNone(self._row(poll_id, order_no)["headway_ahead_sec"])
            self.assertIsNone(self._row(poll_id, order_no)["delay_ahead_sec"])

    def test_deviation_counts_never_see_the_future(self):
        # The seeded feed has one situation for line 12 published 09:30Z and
        # in force all day, plus one published 10:03Z. At poll 1 (10:00:30Z)
        # only the first can be known; at poll 3 (10:05Z) both are.
        self.assertEqual(self._row(1, 2)["sx_line_active"], 1)
        self.assertEqual(self._row(1, 2)["sx_network_active"], 1)
        self.assertEqual(self._row(2, 3)["sx_line_active"], 1)

    def test_freshness_features(self):
        # The fixture stamps every journey RecordedAtTime at 12:00:00+02:00,
        # which is 10:00:00Z, so at poll 1 (10:00:30Z) the feed's picture is
        # 30s old. The vehicle passed stop 1 at 12:00:00+02:00 too.
        row = self._row(1, 2)
        self.assertEqual(row["obs_age_sec"], 30.0)
        self.assertEqual(row["since_last_stop_sec"], 30.0)
        # At poll 2 (10:02:00Z) the last stop passed is stop 2, actual
        # 12:05:30+02:00 = 10:05:30Z, which is in the future relative to the
        # poll: the feed published the arrival before it happened.
        self.assertEqual(self._row(2, 3)["obs_age_sec"], 120.0)
        self.assertEqual(self._row(2, 3)["since_last_stop_sec"], -210.0)

    def test_network_state_reads_only_closed_buckets(self):
        # History is counted into buckets, and a bucket that is still filling
        # could hold observations from after T, so only closed ones are read.
        # With one-minute buckets: the pass at 10:00:30 lands in the 10:00
        # bucket, which is still open at poll 1 (10:00:30) and closed by poll
        # 2 (10:02:00). The freshness cost is exactly one bucket.
        self.assertIsNone(self._row(1, 2)["line_recent_delay_sec"])
        self.assertEqual(self._row(2, 3)["line_recent_delay_sec"], 120.0)
        # Nothing has passed the target stops at all, on any line.
        self.assertIsNone(self._row(1, 2)["stop_recent_delay_sec"])
        self.assertIsNone(self._row(2, 3)["stop_recent_delay_sec"])

    def test_slack_is_null_without_prior_observations(self):
        # Segment 1->2 first becomes observable at poll 2 (10:02Z), segment
        # 2->3 at poll 3 (10:05Z). Every row's poll time T predates the
        # observation of the segments it would need, so slack must be NULL:
        # the as-of-T rule applies to segment history exactly as to weather.
        for poll_id, order_no in ((1, 2), (1, 3), (2, 3)):
            self.assertIsNone(self._row(poll_id, order_no)["seg_slack_sec"])

    def test_weather_join_never_uses_future_forecasts(self):
        # At poll 1 (10:00:30Z) the newest forecast for the 10Z hour was issued
        # 09:59Z (12.0Â°). The 99.0Â° snapshot exists but was issued 10:01Z, in
        # poll 1's future, and must be invisible to poll 1's rows.
        self.assertEqual(self._row(1, 2)["fc_air_temp"], 12.0)
        # At poll 2 (10:02:00Z) the 10:01Z snapshot IS the newest known one.
        self.assertEqual(self._row(2, 3)["fc_air_temp"], 99.0)


def seed_second_journey(path):
    """Later traffic over the same 1->2 segment, then a journey to predict.

    Journeys testB and testC drive the segment fully recorded (runtimes 300s
    and 360s), so together with test1's 330s the segment reaches the
    three-observation minimum: typical runtime mean(330, 300, 360) = 330s.
    Journey test2 is then polled at 10:10Z with stop 2 still ahead."""
    conn = db.connect(path)

    def journey(ref):
        return dict(
            recorded_at=f"{D}T12:20:00+02:00",
            line_ref="RUT:Line:12",
            direction="1",
            journey_ref=ref,
            operating_date=D,
            operator_ref="RUT:Operator:220",
            monitored=1,
            cancelled=0,
            call_cancelled=0,
        )

    b = journey("RUT:ServiceJourney:testB")
    b1 = dict(b, call_type="recorded", stop_ref="NSR:Quay:1", stop_name="A", order_no=1,
              aimed_dep=f"{D}T12:09:00+02:00", actual_dep=f"{D}T12:10:00+02:00")
    b2 = dict(b, call_type="recorded", stop_ref="NSR:Quay:2", stop_name="B", order_no=2,
              aimed_arr=f"{D}T12:14:00+02:00", actual_arr=f"{D}T12:15:00+02:00")
    c = journey("RUT:ServiceJourney:testC")
    c1 = dict(c, call_type="recorded", stop_ref="NSR:Quay:1", stop_name="A", order_no=1,
              aimed_dep=f"{D}T12:11:00+02:00", actual_dep=f"{D}T12:12:00+02:00")
    c2 = dict(c, call_type="recorded", stop_ref="NSR:Quay:2", stop_name="B", order_no=2,
              aimed_arr=f"{D}T12:16:00+02:00", actual_arr=f"{D}T12:18:00+02:00")

    base = journey("RUT:ServiceJourney:test2")
    stop1_recorded = dict(
        base, call_type="recorded", stop_ref="NSR:Quay:1", stop_name="A", order_no=1,
        aimed_dep=f"{D}T12:20:00+02:00", actual_dep=f"{D}T12:21:00+02:00",
    )
    stop2_estimated = dict(
        base, call_type="estimated", stop_ref="NSR:Quay:2", stop_name="B", order_no=2,
        aimed_arr=f"{D}T12:25:00+02:00", expected_arr=f"{D}T12:26:00+02:00",
    )
    stop2_recorded = dict(
        base, call_type="recorded", stop_ref="NSR:Quay:2", stop_name="B", order_no=2,
        aimed_arr=f"{D}T12:25:00+02:00", actual_arr=f"{D}T12:26:30+02:00",
    )
    for polled_at, calls in (
        (f"{D}T10:06:00+00:00", [b1, b2]),
        (f"{D}T10:08:00+00:00", [c1, c2]),
        (f"{D}T10:10:00+00:00", [stop1_recorded, stop2_estimated]),
        (f"{D}T10:12:00+00:00", [stop1_recorded, stop2_recorded]),
    ):
        poll_id = db.insert_poll(conn, polled_at=polled_at, feed="et", dataset="RUT")
        db.insert_calls(conn, [dict(c_, poll_id=poll_id) for c_ in calls])
    conn.close()


class SlackFeatureTest(unittest.TestCase):
    """Slack uses history from OTHER journeys on the same segment, as-of-T."""

    def test_slack_from_prior_journey_runtime(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        archive = Path(tmp.name) / "archive.db"
        out = Path(tmp.name) / "dataset.db"
        seed_archive(archive)
        seed_second_journey(archive)
        build(archive_path=archive, out_path=out,
              parquet_dir=Path(tmp.name) / "no-parquet", bucket_seconds=60)
        conn = db.connect(out)
        self.addCleanup(conn.close)
        row = conn.execute(
            "SELECT sched_runtime_sec, seg_slack_sec, headway_ahead_sec, delay_ahead_sec, "
            "stop_recent_delay_sec, line_recent_delay_sec "
            "FROM training_row "
            "WHERE journey_ref = 'RUT:ServiceJourney:test2' AND order_no = 2"
        ).fetchone()
        self.assertIsNotNone(row)
        # Three prior runtimes over segment 1->2 (330, 300, 360) meet the
        # SLACK_MIN_OBS floor: typical = 330s. test2 has 300s scheduled for
        # the segment: slack = 300 - 330 = -30.
        self.assertEqual(row[0], 300.0)
        self.assertEqual(row[1], -30.0)
        # Bunching is off unless asked for: it is the one feature that needs
        # every individual passing rather than bucket totals.
        self.assertIsNone(row[2])
        self.assertIsNone(row[3])
        # Network state at T=10:10Z, over buckets closed before it. Target
        # stop: 150 (test1), 60 (testB), 120 (testC) -> mean 110. Line: those
        # three plus 120 and 180 from test1 and 60 twice from the others, so
        # 750 over seven. test2's own passing shares T's bucket and is
        # therefore invisible to it.
        self.assertEqual(row[4], 110.0)
        self.assertAlmostEqual(row[5], 750 / 7)

    def test_bunching_is_available_when_asked_for(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        archive = Path(tmp.name) / "archive.db"
        out = Path(tmp.name) / "dataset.db"
        seed_archive(archive)
        seed_second_journey(archive)
        build(archive_path=archive, out_path=out,
              parquet_dir=Path(tmp.name) / "no-parquet", bucket_seconds=60,
              with_bunching=True)
        conn = db.connect(out)
        self.addCleanup(conn.close)
        row = conn.execute(
            "SELECT headway_ahead_sec, delay_ahead_sec FROM training_row "
            "WHERE journey_ref = 'RUT:ServiceJourney:test2' AND order_no = 2"
        ).fetchone()
        # The latest known pass of the target stop at T=10:10Z is testC
        # (observed 10:08Z, actual 12:18, delay 120 against aimed 12:16).
        # test2 expects 12:26:00, so the predicted headway is 480s.
        self.assertEqual(row[0], 480.0)
        self.assertEqual(row[1], 120.0)


class SampleTest(unittest.TestCase):
    """Sampling keeps whole journeys, deterministically."""

    def test_no_sample_keeps_everything(self):
        from punktlig.dataset import _sample_clause

        self.assertEqual(_sample_clause(0), "")
        self.assertEqual(_sample_clause(16), "")

    def test_sample_selects_by_the_last_character_of_the_journey_ref(self):
        from punktlig.dataset import _sample_clause

        clause = _sample_clause(4)
        self.assertIn("substr(c.journey_ref, -1)", clause)
        for digit in "0123":
            self.assertIn(f"'{digit}'", clause)
        for digit in "456789abcdef":
            self.assertNotIn(f"'{digit}'", clause)

    def test_a_sampled_build_returns_fewer_rows_but_still_builds(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        archive = Path(tmp.name) / "archive.db"
        seed_archive(archive)
        # test1 ends in '1', so a one-sixteenth sample of '0' excludes it.
        written = build(archive_path=archive, out_path=Path(tmp.name) / "s.db",
                        parquet_dir=Path(tmp.name) / "none", sample=1)
        self.assertEqual(written, 0)
        written_all = build(archive_path=archive, out_path=Path(tmp.name) / "a.db",
                            parquet_dir=Path(tmp.name) / "none", sample=2)
        self.assertEqual(written_all, 3)


if __name__ == "__main__":
    unittest.main()

