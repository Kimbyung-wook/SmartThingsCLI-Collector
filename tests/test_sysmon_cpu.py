"""CPU 사용률: /proc/stat 두 표본의 차이, 1분 요약 열, 이전 DB 마이그레이션."""

import http.client
import ipaddress
import json
import sqlite3
import threading
import time
import unittest

from stcollector.settings import SysmonSettings, WebSettings, sysmon_db_path
from stcollector.sysmon import (Monitor, Sample, SysmonStore, SystemReader, cpu_percent, parse_core_lines,
                                parse_cpu_line, write_runtime)
from stcollector.web import server
from stcollector.web.session import hash_password
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


def stat_cores(cores: dict) -> str:
    """cores: {번호: (user, idle)}. 합계 줄과 다른 줄도 붙인다."""
    lines = [f"cpu  {sum(u for u, _ in cores.values())} 0 0 {sum(i for _, i in cores.values())} 0 0 0 0 0 0"]
    lines += [f"cpu{n} {u} 0 0 {i} 0 0 0 0 0 0" for n, (u, i) in cores.items()]
    return "\n".join(lines + ["intr 1 2 3", "ctxt 9"]) + "\n"


class CoreParseTests(unittest.TestCase):
    def test_parse_core_lines_skips_total_and_others(self):
        text = stat_cores({0: (10, 90), 2: (30, 70)})  # 코어 1 오프라인
        self.assertEqual(parse_core_lines(text), {"0": (100, 90), "2": (100, 70)})

    def test_garbage_core_line_is_ignored(self):
        self.assertEqual(parse_core_lines("cpu0 a b c d e\ncpu1 1 2\ncpux 1 2 3 4 5\n"), {})


class CoreReaderTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        (self.tmp / "proc").mkdir()
        self.reader = SystemReader(self.tmp / "proc", self.tmp / "sys", throttled_path=self.tmp / "none", disk_path=self.tmp)

    def read(self, cores, ts):
        (self.tmp / "proc" / "stat").write_text(stat_cores(cores))
        return self.reader.sample(ts)

    def test_per_core_and_offline_core(self):
        self.assertIsNone(self.read({0: (0, 0), 1: (0, 0), 2: (0, 0)}, T0).cpu_cores)
        s = self.read({0: (50, 50), 1: (10, 90), 2: (100, 0)}, T0 + STEP)
        self.assertEqual(s.cpu_cores, {"0": 50.0, "1": 10.0, "2": 100.0})
        # 코어 1 이 꺼지면 빠지고, 다시 켜지면 직전 값이 없어 한 표본 쉬었다 나온다
        s = self.read({0: (100, 100), 2: (150, 50)}, T0 + 2 * STEP)
        self.assertEqual(s.cpu_cores, {"0": 50.0, "2": 50.0})
        s = self.read({0: (150, 150), 1: (10, 90), 2: (200, 100)}, T0 + 3 * STEP)
        self.assertEqual(set(s.cpu_cores), {"0", "2"})
        s = self.read({0: (200, 200), 1: (30, 170), 2: (250, 150)}, T0 + 4 * STEP)
        self.assertEqual(s.cpu_cores["1"], 20.0)

    def test_no_core_lines(self):
        (self.tmp / "proc" / "stat").write_text("cpu  1 0 0 9 0 0 0 0\n")
        self.reader.sample(T0)
        (self.tmp / "proc" / "stat").write_text("cpu  2 0 0 18 0 0 0 0\n")
        s = self.reader.sample(T0 + STEP)
        self.assertEqual(s.cpu_pct, 10.0)
        self.assertIsNone(s.cpu_cores)


class CoreMinuteTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store = SysmonStore(self.tmp / "s.db")
        self.monitor = Monitor(self.store, SysmonSettings(flush_s=600), 100)

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def test_minute_json_keyed_by_core_number(self):
        for i, cores in enumerate([{"0": 10.0, "10": 40.0}, None, {"0": 30.0, "2": 5.0}]):
            self.monitor.step(Sample(ts_ms=T0 + i * STEP, cpu_cores=cores), {})
        self.monitor.step(Sample(ts_ms=T0 + 60_000), {})
        self.monitor.flush()
        raw = self.store.conn.execute("SELECT cpu_cores FROM minutes WHERE ts_ms=?", (T0,)).fetchone()[0]
        self.assertEqual(raw, '{"0":20.0,"2":5.0,"10":40.0}')  # 숫자 순서, 코어마다 있던 표본만 평균

    def test_minute_without_cores_is_null(self):
        self.monitor.step(Sample(ts_ms=T0, cpu_pct=5.0), {})
        self.monitor.step(Sample(ts_ms=T0 + 60_000), {})
        self.monitor.flush()
        self.assertIsNone(self.store.conn.execute("SELECT cpu_cores FROM minutes WHERE ts_ms=?", (T0,)).fetchone()[0])


class CoreMigrationTests(TempDirMixin, unittest.TestCase):
    def test_adds_cpu_cores_when_only_cpu_avg_exists(self):
        path = self.tmp / "mid.db"
        SysmonStore(path).close()
        conn = sqlite3.connect(path)
        conn.execute("ALTER TABLE minutes DROP COLUMN cpu_cores")
        conn.commit()
        conn.close()
        store = SysmonStore(path)
        self.assertIn("cpu_cores", {r[1] for r in store.conn.execute("PRAGMA table_info(minutes)")})
        store.close()


class CoreApiTests(TempDirMixin, unittest.TestCase):
    def start(self, old_db=False):
        settings = WebSettings("127.0.0.1", 0, hash_password("x-test-only"), (ipaddress.ip_network("127.0.0.0/8"),), 30)
        db_path = self.tmp / "sensors.db"
        now = int(time.time() * 1000)
        self.minute = now - now % 60_000
        base = dict(temp_avg=55.0, temp_min=54.0, temp_max=57.0, freq_avg=900.0, load_avg=1.0,
                    mem_avail_mb=500, disk_free_gb=20.0, undervolt_s=0, overheat_s=0, throttle_s=0, samples=12)
        store = SysmonStore(sysmon_db_path(db_path))
        store.add_minutes([
            dict(base, ts_ms=self.minute - 180_000, cpu_avg=20.0, cpu_cores='{"0":10.0,"1":30.0}'),
            dict(base, ts_ms=self.minute - 120_000, cpu_avg=40.0, cpu_cores='{"0":50.0,"3":30.0}'),
            dict(base, ts_ms=self.minute - 60_000, cpu_avg=None, cpu_cores="not json"),
        ])
        store.close()
        if old_db:  # 모니터를 새 판으로 재시작하기 전: 두 열이 없는 DB
            conn = sqlite3.connect(sysmon_db_path(db_path))
            conn.execute("ALTER TABLE minutes DROP COLUMN cpu_cores")
            conn.execute("ALTER TABLE minutes DROP COLUMN cpu_avg")
            conn.commit()
            conn.close()
        current = self.tmp / "current.json"
        write_runtime(current, {"ts_ms": now, "temp_c": 51.0, "cpu_pct": 12.0, "cpu_pct_1m": 15.5,
                                "cpu_cores": {"0": 20.0, "1": 4.0}, "top": [], "active": [],
                                "pending": [dict(base, ts_ms=self.minute, cpu_avg=8.0, cpu_cores='{"1":8.0}')]})
        app = server.App(db_path, settings, sysmon_current=current)
        app.sessions.valid = lambda token: token == "ok"
        self.srv = server.Server(("127.0.0.1", 0), app)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request("GET", path, headers={"Cookie": "st_session=ok"})
        r = c.getresponse()
        body = r.read()
        c.close()
        self.assertEqual(r.status, 200)
        return json.loads(body)

    def test_per_core_series_one_hour(self):
        self.start()
        d = self.get("/api/system?period=1h")
        q = d["series"]
        i = q["buckets"].index(self.minute - 120_000)
        self.assertEqual(list(q["cpu_cores"]), ["0", "1", "3"])
        self.assertEqual(q["cpu"][i - 1:i + 2], [20.0, 40.0, None])
        self.assertEqual(q["cpu_cores"]["0"][i - 1:i + 2], [10.0, 50.0, None])  # 깨진 JSON 은 null
        self.assertEqual(q["cpu_cores"]["1"][i - 1:i + 1], [30.0, None])  # 꺼진 코어는 null
        self.assertEqual(q["cpu_cores"]["3"][i], 30.0)
        self.assertTrue(all(len(v) == 180 for v in q["cpu_cores"].values()))
        self.assertEqual(d["current"]["cpu_pct"], 12.0)
        self.assertEqual(d["current"]["cpu_pct_1m"], 15.5)
        self.assertEqual(d["current"]["cpu_cores"], {"0": 20.0, "1": 4.0})

    def test_unsaved_minute_in_24h(self):
        self.start()
        q = self.get("/api/system")["series"]
        bucket = self.minute - self.minute % 300_000
        i = q["buckets"].index(bucket)
        # 아직 DB에 없는 분(8.0). 실행 시각에 따라 3분 전 분(코어 1 = 30.0)이 같은 5분 칸에 들 수 있다.
        vals = [8.0] + ([30.0] if self.minute - 180_000 >= bucket else [])
        self.assertEqual(q["cpu_cores"]["1"][i], sum(vals) / len(vals))

    def test_old_db_without_columns(self):
        self.start(old_db=True)
        q = self.get("/api/system?period=1h")["series"]
        self.assertIn(55.0, q["temp_avg"])
        self.assertTrue(all(v is None for v in q["cpu"]))
        self.assertEqual(q["cpu_cores"], {})


if __name__ == "__main__":
    unittest.main()
