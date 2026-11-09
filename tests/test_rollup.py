"""집계 규칙 검증 (PLAN §7)."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from stcollector.db import Database
from stcollector.rollup import (
    DAY_MS, HOUR_MS, KST_OFFSET_MS, LUX_LABELS, floor_hour, kst_day_start, kst_week_start, lux_bin, run_rollup,
)
from stcollector.settings import RollupSettings

# 2026-01-05 은 월요일. BASE 는 그날 00:00 KST (= 2026-01-04 15:00 UTC).
BASE = 1_767_538_800_000
MIN = 60_000


def temp(ts, value, device="d1", attr="temperature", cap="temperatureMeasurement", component="main"):
    return (device, component, cap, attr, ts, value, None, "C", "history", None)


def text(ts, value, device, attr, cap):
    return (device, "main", cap, attr, ts, None, value, None, "history", None)


class Base(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.settings = RollupSettings(retention_days=0)

    def tearDown(self):
        self.db.close()

    def run_at(self, now, **kw):
        return run_rollup(self.db, self.settings, now_ms=now, **kw)

    def row(self, table, bucket, attr="temperature", device="d1"):
        r = self.db.conn.execute(
            f"SELECT avg, min, max, n, coverage, last_val FROM {table} WHERE device_id=? AND attribute=? AND bucket_start=?",
            (device, attr, bucket),
        ).fetchone()
        return tuple(r) if r else None

    def dump(self):
        out = {}
        for table in ("rollup_hour", "rollup_day", "rollup_week", "lux_hist_hour", "occupancy_hour", "lux_occ_hour"):
            out[table] = [tuple(r) for r in self.db.conn.execute(f"SELECT * FROM {table} ORDER BY 1,2,3,4")]
        return out


class BoundaryTests(unittest.TestCase):
    def test_base_is_monday_midnight_kst(self):
        self.assertEqual((BASE + KST_OFFSET_MS) % DAY_MS, 0)
        self.assertEqual(kst_week_start(BASE), BASE)

    def test_kst_day_boundary(self):
        self.assertEqual(kst_day_start(BASE + 1), BASE)
        self.assertEqual(kst_day_start(BASE - 1), BASE - DAY_MS)
        self.assertEqual(kst_day_start(BASE + DAY_MS - 1), BASE)

    def test_week_starts_monday_kst(self):
        self.assertEqual(kst_week_start(BASE + 6 * DAY_MS + 23 * HOUR_MS), BASE)  # 일요일 23시
        self.assertEqual(kst_week_start(BASE - 1), BASE - 7 * DAY_MS)  # 직전 일요일 밤은 이전 주
        self.assertEqual(kst_week_start(BASE + 7 * DAY_MS), BASE + 7 * DAY_MS)

    def test_lux_bins(self):
        self.assertEqual(len(LUX_LABELS), 13)
        cases = {-1: 0, 0: 0, 0.99: 0, 1: 1, 4.9: 1, 5: 2, 99.9: 5, 100: 6, 999: 8, 1000: 9, 9999: 11, 10000: 12, 50000: 12}
        for value, expected in cases.items():
            self.assertEqual(lux_bin(value), expected, value)


class RollupTests(Base):
    def test_time_weighted_not_count_weighted(self):
        rows = [temp(BASE, 20.0)] + [temp(BASE + 30 * MIN + i * MIN, 30.0) for i in range(30)]
        self.db.insert_readings(rows)
        self.run_at(BASE + HOUR_MS + 1)
        avg, lo, hi, n, cov, last = self.row("rollup_hour", BASE)
        self.assertAlmostEqual(avg, 25.0)  # 단순 평균이면 약 29.7
        self.assertEqual((lo, hi, n, cov, last), (20.0, 30.0, 31, 1.0, 30.0))

    def test_value_held_into_next_hour_keeps_min_max_consistent(self):
        self.db.insert_readings([temp(BASE + 50 * MIN, 20.0), temp(BASE + HOUR_MS + 30 * MIN, 30.0)])
        self.run_at(BASE + 2 * HOUR_MS + 1)
        avg, lo, hi, n, cov, _ = self.row("rollup_hour", BASE + HOUR_MS)
        self.assertAlmostEqual(avg, 25.0)
        self.assertEqual((lo, hi, n, cov), (20.0, 30.0, 1, 1.0))
        self.assertLessEqual(lo, avg)
        self.assertLessEqual(avg, hi)

    def test_hour_without_reports_is_filled_from_held_value(self):
        self.db.insert_readings([temp(BASE, 21.0)])
        self.run_at(BASE + 2 * HOUR_MS + 1)
        self.assertEqual(self.row("rollup_hour", BASE + HOUR_MS)[:5], (21.0, 21.0, 21.0, 0, 1.0))

    def test_coverage_for_gap_beyond_max_hold(self):
        self.settings = RollupSettings(retention_days=0, max_hold_s={"temperature": 1800})
        self.db.insert_readings([temp(BASE, 21.0), temp(BASE + 3 * HOUR_MS, 25.0)])
        self.run_at(BASE + 4 * HOUR_MS + 1)
        self.assertAlmostEqual(self.row("rollup_hour", BASE)[4], 0.5)  # 30분만 유지
        self.assertIsNone(self.row("rollup_hour", BASE + HOUR_MS))
        self.assertIsNone(self.row("rollup_hour", BASE + 2 * HOUR_MS))
        self.assertAlmostEqual(self.row("rollup_hour", BASE + 3 * HOUR_MS)[4], 0.5)

    def test_day_is_weighted_by_covered_time(self):
        self.settings = RollupSettings(retention_days=0, max_hold_s={"temperature": 1800})
        self.db.insert_readings([temp(BASE, 10.0), temp(BASE + 2 * HOUR_MS, 20.0), temp(BASE + 2 * HOUR_MS + 30 * MIN, 20.0)])
        self.run_at(BASE + DAY_MS + 1)
        avg, lo, hi, n, cov, _ = self.row("rollup_day", BASE)
        self.assertAlmostEqual(avg, (10 * 0.5 + 20 * 1.0) / 1.5)
        self.assertEqual((lo, hi, n), (10.0, 20.0, 3))
        self.assertAlmostEqual(cov, 1.5 / 24)

    def test_kst_day_and_week_assignment(self):
        self.db.insert_readings([temp(BASE - 30 * MIN, 10.0), temp(BASE + 30 * MIN, 20.0)])
        self.run_at(BASE + 2 * HOUR_MS)
        self.assertIsNotNone(self.row("rollup_day", BASE - DAY_MS))  # 일요일 23:30 KST
        self.assertIsNotNone(self.row("rollup_day", BASE))
        self.assertIsNotNone(self.row("rollup_week", BASE - 7 * DAY_MS))
        self.assertIsNotNone(self.row("rollup_week", BASE))

    def test_open_hour_not_aggregated(self):
        self.db.insert_readings([temp(BASE + 10 * MIN, 21.0)])
        self.run_at(BASE + 30 * MIN)
        self.assertIsNone(self.row("rollup_hour", BASE))

    def test_idempotent(self):
        self.db.insert_readings([temp(BASE + i * 7 * MIN, 20.0 + i % 5) for i in range(100)])
        self.db.insert_readings([temp(BASE + i * 11 * MIN, 100.0 + i, device="l1", attr="illuminance",
                                      cap="illuminanceMeasurement") for i in range(50)])
        now = BASE + 20 * HOUR_MS + 5
        self.run_at(now)
        first = self.dump()
        self.run_at(now)
        self.assertEqual(first, self.dump())
        self.run_at(now, full=True)
        self.assertEqual(first, self.dump())

    def test_incremental_equals_full_rebuild(self):
        rows = [temp(BASE + i * 13 * MIN, 20.0 + (i % 7) / 2) for i in range(300)]
        rows += [temp(BASE + i * 9 * MIN, float(i % 400), device="l1", attr="illuminance",
                      cap="illuminanceMeasurement") for i in range(400)]
        rows.sort(key=lambda r: r[4])
        now = BASE
        for start in range(0, len(rows), 100):  # 조금씩 들어오는 상황
            self.db.insert_readings(rows[start:start + 100])
            now = floor_hour(rows[min(start + 99, len(rows) - 1)][4]) + HOUR_MS
            self.run_at(now + 5)
        incremental = self.dump()
        self.run_at(now + 5, full=True)
        self.assertEqual(incremental, self.dump())

    def test_late_data_recomputes_past_hours(self):
        self.db.insert_readings([temp(BASE, 10.0)])
        self.run_at(BASE + 3 * HOUR_MS)
        self.assertEqual(self.row("rollup_hour", BASE + HOUR_MS)[0], 10.0)
        self.db.insert_readings([temp(BASE + HOUR_MS, 30.0)])  # 늦게 도착
        self.run_at(BASE + 3 * HOUR_MS + 1)
        self.assertEqual(self.row("rollup_hour", BASE + HOUR_MS)[0], 30.0)
        self.assertEqual(self.row("rollup_hour", BASE + 2 * HOUR_MS)[0], 30.0)
        self.assertEqual(self.row("rollup_hour", BASE)[0], 10.0)

    def test_unchanged_run_writes_only_meta(self):
        self.db.insert_readings([temp(BASE, 21.0)])
        self.run_at(BASE + 2 * HOUR_MS)
        before = self.db.conn.total_changes
        self.run_at(BASE + 2 * HOUR_MS + 10)
        self.assertLessEqual(self.db.conn.total_changes - before, 2)  # 집계 시각 meta 2건뿐

    def test_arrival_during_rollup_is_not_lost(self):
        self.db.insert_readings([temp(BASE, 10.0)])
        self.db.conn.execute("INSERT OR IGNORE INTO rollup_claimed SELECT * FROM rollup_dirty")
        self.db.conn.execute("DELETE FROM rollup_dirty")
        self.db.conn.commit()
        self.db.insert_readings([temp(BASE + MIN, 20.0)])  # 같은 시간대에 처리 중 도착
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM rollup_dirty").fetchone()[0], 1)

    def test_insert_count_ignores_trigger_rows(self):
        self.assertEqual(self.db.insert_readings([temp(BASE, 1.0), temp(BASE + 1, 2.0)]), 2)
        self.assertEqual(self.db.insert_readings([temp(BASE, 1.0)]), 0)

    def test_prefers_main_component(self):
        self.db.insert_readings([temp(BASE, 10.0, component="aux"), temp(BASE, 20.0, component="main")])
        self.run_at(BASE + HOUR_MS)
        self.assertEqual(self.row("rollup_hour", BASE)[0], 20.0)

    def test_switch_on_fraction(self):
        self.db.insert_readings([text(BASE, "on", "p1", "switch", "switch"),
                                 text(BASE + 15 * MIN, "off", "p1", "switch", "switch")])
        self.run_at(BASE + HOUR_MS)
        self.assertAlmostEqual(self.row("rollup_hour", BASE, attr="switch", device="p1")[0], 0.25)


class LuxOccupancyTests(Base):
    def lux(self, ts, value, device="l1"):
        return temp(ts, value, device=device, attr="illuminance", cap="illuminanceMeasurement")

    def bins(self, bucket):
        return {r[0]: r[1] for r in self.db.conn.execute(
            "SELECT lux_bin, seconds FROM lux_hist_hour WHERE device_id='l1' AND bucket_start=?", (bucket,))}

    def test_dwell_seconds_by_bin(self):
        self.db.insert_readings([self.lux(BASE, 3.0), self.lux(BASE + 20 * MIN, 150.0), self.lux(BASE + 50 * MIN, 12000.0)])
        self.run_at(BASE + HOUR_MS)
        self.assertEqual(self.bins(BASE), {1: 1200.0, 6: 1800.0, 12: 600.0})

    def test_many_reports_do_not_inflate_a_bin(self):
        self.db.insert_readings([self.lux(BASE, 3.0)] + [self.lux(BASE + 55 * MIN + i * 1000, 150.0) for i in range(100)])
        self.run_at(BASE + HOUR_MS)
        rows = self.bins(BASE)
        self.assertEqual(rows[1], 3300.0)
        self.assertEqual(rows[6], 300.0)

    def test_occupancy_seconds(self):
        cap = "motionSensor"
        self.db.insert_readings([text(BASE, "inactive", "o1", "motion", cap),
                                 text(BASE + 10 * MIN, "active", "o1", "motion", cap),
                                 text(BASE + 40 * MIN, "inactive", "o1", "motion", cap)])
        self.run_at(BASE + HOUR_MS)
        r = self.db.conn.execute("SELECT occupied_seconds, known_seconds FROM occupancy_hour WHERE device_id='o1'").fetchone()
        self.assertEqual(tuple(r), (1800.0, 3600.0))

    def test_lux_while_occupied(self):
        self.db.upsert_devices([
            {"device_id": d, "label": d, "name": d, "room_id": "r", "room_name": "방", "location_id": "x", "capabilities": {"switch"}}
            for d in ("o1", "l1")
        ])
        self.db.insert_readings([self.lux(BASE, 3.0), self.lux(BASE + 30 * MIN, 150.0),
                                 text(BASE + 20 * MIN, "active", "o1", "motion", "motionSensor"),
                                 text(BASE + 40 * MIN, "inactive", "o1", "motion", "motionSensor")])
        self.run_at(BASE + HOUR_MS)
        rows = {r[0]: r[1] for r in self.db.conn.execute("SELECT lux_bin, seconds FROM lux_occ_hour")}
        self.assertEqual(rows, {1: 600.0, 6: 600.0})


class RetentionTests(Base):
    def test_old_readings_deleted_rollups_kept(self):
        self.settings = RollupSettings(retention_days=30)
        old = BASE - 60 * DAY_MS
        self.db.insert_readings([temp(old, 10.0), temp(old + 10 * MIN, 11.0), temp(BASE, 20.0)])
        self.run_at(BASE + HOUR_MS)
        self.assertEqual([r[0] for r in self.db.conn.execute("SELECT ts_ms FROM readings")], [BASE])

    def test_latest_report_of_quiet_device_is_kept(self):
        self.settings = RollupSettings(retention_days=30)
        self.db.insert_readings([text(BASE - 60 * DAY_MS, "on", "p1", "switch", "switch")])
        self.run_at(BASE + HOUR_MS)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 1)

    def test_retention_zero_keeps_everything(self):
        self.db.insert_readings([temp(BASE - 900 * DAY_MS, 10.0), temp(BASE, 20.0)])
        self.run_at(BASE + HOUR_MS)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 2)


class SchemaMigrationTests(unittest.TestCase):
    def test_existing_db_gets_rollup_tables_and_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.executescript(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
                "CREATE TABLE readings(device_id TEXT NOT NULL, component TEXT NOT NULL, capability TEXT NOT NULL,"
                " attribute TEXT NOT NULL, ts_ms INTEGER NOT NULL, value_num REAL, value_text TEXT, unit TEXT,"
                " source TEXT NOT NULL, event_hash INTEGER,"
                " PRIMARY KEY(device_id, component, capability, attribute, ts_ms)) WITHOUT ROWID;"
            )
            conn.execute("INSERT INTO readings VALUES('d1','main','temperatureMeasurement','temperature',?,21.0,NULL,'C','status',NULL)", (BASE,))
            conn.commit()
            conn.close()
            db = Database(path)
            run_rollup(db, RollupSettings(retention_days=0), now_ms=BASE + HOUR_MS)
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM rollup_hour").fetchone()[0], 1)
            db.insert_readings([("d1", "main", "temperatureMeasurement", "temperature", BASE + HOUR_MS + 5, 22.0, None, "C", "status", None)])
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM rollup_dirty").fetchone()[0], 1)
            db.close()


if __name__ == "__main__":
    unittest.main()
