"""끄는 차트: sysmon 1시간 요약(hours), /api/series·/api/system 의 end 묶음."""

import json
import sqlite3
import unittest

from stcollector.db import Database
from stcollector.settings import SysmonSettings, sysmon_db_path
from stcollector.sysmon import Monitor, Sample, SysmonStore
from stcollector.web import api
from tests.helpers import TempDirMixin

H = 3_600_000
DAY = 24 * H
KST = 9 * H
NOW = 1_767_538_800_000 + 30 * 60_000  # 정시 30분 뒤
HZ = 100


def minute_row(ts, temp=50.0, cores=None, mem=500, disk=20.0, uv=0):
    return dict(ts_ms=ts, temp_avg=temp, temp_min=temp - 1, temp_max=temp + 1, freq_avg=900.0, load_avg=0.5,
                cpu_avg=10.0, cpu_cores=json.dumps(cores) if cores is not None else None, mem_avail_mb=mem,
                disk_free_gb=disk, undervolt_s=uv, overheat_s=0, throttle_s=0, samples=12)


class HoursTableTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store = SysmonStore(self.tmp / "s.db")

    def tearDown(self):
        self.store.close()  # 임시 폴더를 지우기 전에 닫는다 (Windows)
        super().tearDown()

    def hours(self):
        return self.store.conn.execute("SELECT * FROM hours ORDER BY ts_ms").fetchall()

    def test_hour_row_values(self):
        h = NOW - NOW % H - 2 * H
        self.store.add_minutes([
            minute_row(h, temp=50.0, cores={"0": 10.0, "1": 30.0}, mem=600, disk=21.0, uv=5),
            minute_row(h + 60_000, temp=52.0, cores={"0": 20.0}, mem=480, disk=20.5, uv=7),
            minute_row(h + H, temp=40.0),  # 다음 시간: 아직 끝나지 않았으면 넣지 않는다
        ])
        self.assertEqual(self.store.rollup_hours(h + H + 30 * 60_000), 1)
        row = self.hours()[0]
        self.assertEqual(row[0], h)
        self.assertEqual(row[1:4], (51.0, 49.0, 53.0))
        self.assertEqual(json.loads(row[7]), {"0": 15.0, "1": 30.0})
        self.assertEqual(row[8:], (480, 20.5, 12, 0, 0, 2))
        self.assertEqual(len(self.hours()), 1)
        self.assertEqual(self.store.rollup_hours(h + 2 * H), 1)  # 이어서: 다음 시간만
        self.assertEqual([r[0] for r in self.hours()], [h, h + H])
        self.assertEqual(self.store.rollup_hours(h + 2 * H), 0)

    def test_first_run_backfills_all_minutes_and_hours_are_kept(self):
        start = NOW - NOW % H - 20 * DAY
        self.store.add_minutes([minute_row(start + i * 10 * 60_000) for i in range(20 * 24 * 6)])
        self.assertEqual(self.store.rollup_hours(NOW), 20 * 24)
        self.store.apply_retention(NOW, 14, 365)
        self.assertEqual(len(self.hours()), 20 * 24)
        oldest_minute = self.store.conn.execute("SELECT MIN(ts_ms) FROM minutes").fetchone()[0]
        self.assertGreaterEqual(oldest_minute, NOW - 14 * DAY)

    def test_empty_minutes(self):
        self.assertEqual(self.store.rollup_hours(NOW), 0)
        self.assertIsNone(self.store.get_meta("hours_done"))

    def test_monitor_begin_backfills_and_flush_adds_finished_hour(self):
        h = NOW - NOW % H
        self.store.add_minutes([minute_row(h - H + i * 60_000) for i in range(60)])
        monitor = Monitor(self.store, SysmonSettings(flush_s=60), HZ)
        monitor.begin("boot-1", h + 59 * 60_000, 30)
        self.assertEqual([r[0] for r in self.hours()], [h - H])
        ts = h + 59 * 60_000
        while ts < h + H + 2 * 60_000:  # 정시를 넘겨 다음 flush 까지
            monitor.step(Sample(ts_ms=ts, temp_c=48.0, freq_mhz=900.0, load1=0.3), {})
            ts += 5000
        rows = self.hours()
        self.assertEqual([r[0] for r in rows], [h - H, h])
        self.assertEqual(rows[1][-1], 1)  # 그 시간에 기록된 분은 59분 하나뿐


class SeriesChunkTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        path = self.tmp / "s.db"
        Database(path).close()
        self.conn = sqlite3.connect(str(path))
        self.closed = NOW - NOW % H
        self.first = self.closed - 10 * DAY
        c = self.conn
        c.execute("INSERT INTO rollup_series VALUES ('d1','main','temperatureMeasurement','temperature','C')")
        rows = [("d1", "temperature", t, 20.0, 19.0, 21.0, 20.0, 12, 1.0) for t in range(self.first, self.closed, H)]
        c.executemany("INSERT INTO rollup_hour VALUES (?,?,?,?,?,?,?,?,?)", rows)
        day0 = self.first - (self.first + KST) % DAY
        c.executemany("INSERT INTO rollup_day VALUES (?,?,?,?,?,?,?,?,?)",
                      [("d1", "temperature", t, 20.0, 19.0, 21.0, 20.0, 12, 1.0) for t in range(day0, self.closed, DAY)])
        c.execute("INSERT INTO meta VALUES ('rollup_closed_until_ms', ?)", (str(self.closed),))
        c.commit()
        self.ctx = api.Context(self.conn, NOW, path)

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def get(self, **params):
        return api.series(self.ctx, {"metric": "temperature", **{k: str(v) for k, v in params.items()}})

    def test_latest_chunk_is_three_days(self):
        d = self.get(period="24h")
        self.assertEqual(len(d["buckets"]), 72)
        self.assertEqual(d["buckets"][-1], self.closed - H)
        self.assertEqual(d["view"], 24)
        self.assertEqual(d["oldest_ms"], self.first)
        self.assertTrue(d["has_older"])
        self.assertEqual(d["series"][0]["avg"][-1], 20.0)

    def test_older_chunks_join_without_gap_until_first_record(self):
        d = self.get(period="24h")
        seen = list(d["buckets"])
        while d["has_older"]:
            d = self.get(period="24h", end=d["buckets"][0])
            self.assertEqual(d["buckets"][-1] + H, seen[0])
            seen = d["buckets"] + seen
        self.assertLessEqual(d["buckets"][0], self.first)
        self.assertEqual(seen, list(range(seen[0], self.closed, H)))
        self.assertEqual(d["series"][0]["avg"][d["buckets"].index(self.first) - 1], None)

    def test_end_after_latest_is_clamped(self):
        self.assertEqual(self.get(period="24h", end=NOW + 30 * DAY)["buckets"], self.get(period="24h")["buckets"])

    def test_month_and_year_chunks(self):
        m = self.get(period="month")
        self.assertEqual(len(m["buckets"]), 90)
        self.assertTrue(all((b + KST) % DAY == 0 for b in m["buckets"]))
        self.assertEqual(m["series"][0]["avg"][-2], 20.0)  # 마지막 칸은 오늘(아직 집계 전)
        older = self.get(period="month", end=m["buckets"][0])
        self.assertEqual(older["buckets"][-1] + DAY, m["buckets"][0])
        self.assertFalse(older["has_older"])
        y = self.get(period="year")
        self.assertEqual(len(y["buckets"]), 156)
        self.assertEqual(y["buckets"][1] - y["buckets"][0], 7 * DAY)

    def test_bad_end(self):
        for bad in ("abc", "-1", "1e12", "9" * 16, "1.5"):
            with self.assertRaises(api.BadRequest, msg=bad):
                self.get(period="24h", end=bad)


class SystemChunkTests(TempDirMixin, unittest.TestCase):
    """1분 요약은 최근 2일, 1시간 요약은 그 전 8일부터 있다 (보관 기간이 지난 상태)."""

    def setUp(self):
        super().setUp()
        self.db_path = self.tmp / "sensors.db"
        store = SysmonStore(sysmon_db_path(self.db_path))
        minute_now = NOW - NOW % 60_000
        self.minute_from = minute_now - 2 * DAY + 7 * 60_000  # 정시가 아닌 곳에서 시작
        store.add_minutes([minute_row(t, temp=50.0, cores={"0": 10.0}) for t in range(self.minute_from, minute_now, 60_000)])
        store.rollup_hours(minute_now)
        self.hour_from = self.minute_from - self.minute_from % H - 8 * DAY
        with store.conn:
            store.conn.executemany(
                "INSERT INTO hours VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(t, 40.0, 39.0, 41.0, 800.0, 0.4, 5.0, '{"0":5.0}', 500, 20.0, 0, 0, 0, 60)
                 for t in range(self.hour_from, self.minute_from - self.minute_from % H, H)])
        store.close()
        self.ctx = api.Context(None, NOW, self.db_path, sysmon_db_path(self.db_path), self.tmp / "none.json")
        self.split = self.minute_from - self.minute_from % H + H

    def get(self, **params):
        return api.system(self.ctx, {k: str(v) for k, v in params.items()})

    def test_latest_chunk_switches_to_hours_before_minutes(self):
        d = self.get(period="24h")
        b = d["series"]["buckets"]
        self.assertEqual(d["hourly_until"], self.split)
        self.assertEqual(d["end"], b[-1] + 5 * 60_000)
        i = b.index(self.split)
        self.assertTrue(all(y - x == H for x, y in zip(b[:i], b[1:i + 1])))
        self.assertTrue(all(y - x == 5 * 60_000 for x, y in zip(b[i:], b[i + 1:])))
        temps = d["series"]["temp_avg"]
        self.assertEqual(temps[i - 2], 40.0)  # 1시간 요약 (보관 기간이 지난 시간)
        self.assertEqual(temps[i], 50.0)  # 1분 요약
        self.assertEqual(d["series"]["coverage"][i - 2], 1.0)
        self.assertEqual(d["series"]["cpu_cores"]["0"][i - 2], 5.0)
        self.assertEqual(d["oldest_ms"], self.hour_from)
        self.assertTrue(d["has_older"])
        self.assertEqual(d["view_ms"], DAY)

    def test_older_chunks_are_hourly_and_contiguous(self):
        d = self.get(period="24h")
        first = d["series"]["buckets"][0]
        older = self.get(period="24h", end=first)
        b = older["series"]["buckets"]
        self.assertEqual(len(b), 72)
        self.assertEqual(b[-1] + H, first)
        self.assertEqual(older["end"], first)
        while older["has_older"]:
            older = self.get(period="24h", end=older["series"]["buckets"][0])
        self.assertLessEqual(older["series"]["buckets"][0], self.hour_from)

    def test_one_hour_view_stops_at_minutes(self):
        d = self.get(period="1h", end=self.minute_from + 2 * H)
        self.assertEqual(len(d["series"]["buckets"]), 180)
        self.assertEqual(d["oldest_ms"], self.minute_from)
        self.assertFalse(d["has_older"])
        self.assertIsNone(d["hourly_until"])
        self.assertEqual(d["series"]["temp_avg"][0], None)

    def test_bad_end(self):
        with self.assertRaises(api.BadRequest):
            self.get(period="24h", end="soon")


if __name__ == "__main__":
    unittest.main()
