"""CPU 사용률: /proc/stat 두 표본의 차이, 1분 요약 열, 이전 DB 마이그레이션."""

import sqlite3
import unittest

from stcollector.settings import SysmonSettings
from stcollector.sysmon import Monitor, Sample, SysmonStore, SystemReader, cpu_percent, parse_cpu_line
from tests.helpers import TempDirMixin

T0 = 1_767_538_800_000
STEP = 5000


def stat_line(user, nice, system, idle, iowait, irq=0, softirq=0, steal=0, guest=7):
    return f"cpu  {user} {nice} {system} {idle} {iowait} {irq} {softirq} {steal} {guest} 0\ncpu0 1 2 3 4 5 6 7 8 9 0\n"


class CpuParseTests(unittest.TestCase):
    def test_parse_cpu_line(self):
        self.assertEqual(parse_cpu_line(stat_line(10, 0, 5, 80, 5)), (100, 85))

    def test_parse_rejects_garbage(self):
        self.assertIsNone(parse_cpu_line("intr 1 2 3"))
        self.assertIsNone(parse_cpu_line("cpu a b c d e"))
        self.assertIsNone(parse_cpu_line("cpu 1 2"))

    def test_percent(self):
        self.assertEqual(cpu_percent((100, 85), (200, 135)), 50.0)
        self.assertEqual(cpu_percent((100, 85), (200, 185)), 0.0)
        self.assertIsNone(cpu_percent(None, (200, 135)))
        self.assertIsNone(cpu_percent((100, 85), (100, 85)))   # 시간이 흐르지 않음
        self.assertIsNone(cpu_percent((200, 85), (100, 50)))   # 재부팅 등으로 줄어듦


class CpuReaderTests(TempDirMixin, unittest.TestCase):
    def test_first_sample_is_none_then_difference(self):
        proc, sys = self.tmp / "proc", self.tmp / "sys"
        proc.mkdir()
        sys.mkdir()
        reader = SystemReader(proc, sys, throttled_path=self.tmp / "none", disk_path=self.tmp)
        (proc / "stat").write_text(stat_line(10, 0, 5, 80, 5))
        self.assertIsNone(reader.sample(T0).cpu_pct)
        (proc / "stat").write_text(stat_line(40, 0, 5, 140, 15))  # 전체 +100, 쉼 +70
        self.assertEqual(reader.sample(T0 + STEP).cpu_pct, 30.0)

    def test_missing_proc_stat(self):
        reader = SystemReader(self.tmp / "proc", self.tmp / "sys", throttled_path=self.tmp / "none", disk_path=self.tmp)
        self.assertIsNone(reader.sample(T0).cpu_pct)


class CpuMonitorTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store = SysmonStore(self.tmp / "s.db")
        self.monitor = Monitor(self.store, SysmonSettings(flush_s=600), 100)

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def test_minute_average_and_recent_average(self):
        for i, pct in enumerate([10.0, None, 30.0]):
            self.monitor.step(Sample(ts_ms=T0 + i * STEP, cpu_pct=pct), {})
        snap = self.monitor.snapshot(Sample(ts_ms=T0 + 2 * STEP, cpu_pct=30.0))
        self.assertEqual(snap["cpu_pct"], 30.0)
        self.assertEqual(snap["cpu_pct_1m"], 20.0)
        self.monitor.step(Sample(ts_ms=T0 + 60_000, cpu_pct=90.0), {})
        self.assertEqual(self.monitor.cpu_pct_1m(), 60.0)  # 60초 지난 첫 표본(10)은 빠짐
        self.monitor.flush()
        row = self.store.conn.execute("SELECT cpu_avg FROM minutes WHERE ts_ms=?", (T0,)).fetchone()
        self.assertEqual(row[0], 20.0)

    def test_rows_without_cpu_avg_are_accepted(self):
        # 이전 판 모니터가 현재값 파일에 남긴 행에는 cpu_avg 가 없다
        self.store.add_minutes([dict(ts_ms=T0, temp_avg=1, temp_min=1, temp_max=1, freq_avg=1, load_avg=1,
                                     mem_avail_mb=1, disk_free_gb=1, undervolt_s=0, overheat_s=0, throttle_s=0, samples=1)])
        self.assertIsNone(self.store.conn.execute("SELECT cpu_avg FROM minutes").fetchone()[0])


class MigrationTests(TempDirMixin, unittest.TestCase):
    def test_adds_cpu_avg_to_old_db(self):
        path = self.tmp / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE minutes (
                ts_ms INTEGER PRIMARY KEY, temp_avg REAL, temp_min REAL, temp_max REAL, freq_avg REAL,
                load_avg REAL, mem_avail_mb INTEGER, disk_free_gb REAL,
                undervolt_s INTEGER NOT NULL DEFAULT 0, overheat_s INTEGER NOT NULL DEFAULT 0,
                throttle_s INTEGER NOT NULL DEFAULT 0, samples INTEGER NOT NULL
            ) WITHOUT ROWID;
            INSERT INTO minutes(ts_ms, temp_avg, samples) VALUES (1, 50.0, 12);
        """)
        conn.commit()
        conn.close()
        store = SysmonStore(path)
        cols = {r[1] for r in store.conn.execute("PRAGMA table_info(minutes)")}
        self.assertIn("cpu_avg", cols)
        self.assertEqual(store.conn.execute("SELECT temp_avg, cpu_avg FROM minutes").fetchone(), (50.0, None))
        store.close()
        SysmonStore(path).close()  # 두 번 열어도 오류 없음


if __name__ == "__main__":
    unittest.main()
