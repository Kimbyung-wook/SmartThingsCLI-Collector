"""RPi 상태 모니터: 파일 읽기, 이벤트, 저장 절약, 화면 API."""

import http.client
import ipaddress
import json
import threading
import unittest
from pathlib import Path
from unittest import mock

from stcollector import __version__
from stcollector.config import ConfigError
from stcollector.settings import SysmonSettings, WebSettings, load_sysmon_settings, sysmon_db_path
from stcollector.sysmon import Monitor, Sample, SysmonStore, SystemReader, parse_stat, run_monitor, write_runtime
from stcollector.web import server
from stcollector.web.session import hash_password
from tests.helpers import TempDirMixin

T0 = 1_767_538_800_000  # 분 경계
STEP = 5000
HZ = 100
ROOT = Path(__file__).resolve().parent.parent


def sample(ts, **kw):
    return Sample(ts_ms=ts, temp_c=kw.pop("temp_c", 50.0), freq_mhz=900.0, load1=0.5, **kw)


class ParseTests(unittest.TestCase):
    def test_parse_stat_with_odd_name(self):
        line = "123 (a b) c) S 1 2 3 4 5 4194304 7 8 9 10 11 22 0 0 20 0 1 0 555 1000 10 0"
        self.assertEqual(parse_stat(line), ("a b) c", 4194304, 33, 555))

    def test_parse_stat_rejects_garbage(self):
        self.assertIsNone(parse_stat("garbage"))
        self.assertIsNone(parse_stat("1 (x) S 1"))


class ReaderTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.proc, self.sys = self.tmp / "proc", self.tmp / "sys"
        temp = self.sys / "class/hwmon/hwmon0"
        volt = self.sys / "class/hwmon/hwmon1"
        freq = self.sys / "devices/system/cpu/cpu0/cpufreq"
        for d in (temp, volt, freq, self.proc / "sys/kernel/random"):
            d.mkdir(parents=True)
        (temp / "name").write_text("cpu_thermal\n")
        (temp / "temp1_input").write_text("52616\n")
        (volt / "name").write_text("rpi_volt\n")
        (volt / "in0_lcrit_alarm").write_text("1\n")
        (freq / "scaling_cur_freq").write_text("900000\n")
        (self.proc / "loadavg").write_text("0.42 0.3 0.2 1/100 999\n")
        (self.proc / "uptime").write_text("1234.5 100.0\n")
        (self.proc / "meminfo").write_text("MemTotal: 926720 kB\nMemFree: 1 kB\nMemAvailable: 512000 kB\n")
        (self.proc / "sys/kernel/random/boot_id").write_text("boot-a\n")

    def reader(self, **kw):
        return SystemReader(self.proc, self.sys, throttled_path=kw.pop("throttled", self.tmp / "none"), disk_path=self.tmp)

    def add_proc(self, pid, comm, ticks, flags=0, cgroup=None, cmdline=None):
        d = self.proc / str(pid)
        d.mkdir()
        # 필드: state ppid pgrp session tty tpgid flags minflt cminflt majflt cmajflt utime stime ... starttime
        (d / "stat").write_text(f"{pid} ({comm}) S 1 1 1 0 -1 {flags} 0 0 0 0 {ticks} 0 0 0 20 0 1 0 77 1000 10 0\n")
        if cgroup:
            (d / "cgroup").write_text(cgroup)
        if cmdline:
            (d / "cmdline").write_text(cmdline)

    def test_reads_sysfs_without_get_throttled(self):
        r = self.reader()
        s = r.sample(T0)
        self.assertEqual((s.temp_c, s.freq_mhz, s.load1, s.uptime_s), (52.616, 900.0, 0.42, 1234))
        self.assertEqual((s.mem_total_mb, s.mem_avail_mb), (905, 500))
        self.assertTrue(s.undervoltage)
        self.assertIsNone(s.throttled)
        self.assertEqual(r.capabilities, {"undervoltage": True, "throttle_bits": False})

    def test_get_throttled_bits_when_present(self):
        f = self.tmp / "throttled"
        f.write_text("50005\n")
        s = self.reader(throttled=f).sample(T0)
        self.assertTrue(s.undervoltage)
        self.assertTrue(s.throttled)
        self.assertFalse(s.freq_capped)
        self.assertFalse(s.soft_temp)

    def test_processes_skip_kernel_threads_and_label_python_units(self):
        self.add_proc(10, "kworker/0:1", 5, flags=0x00200000)
        self.add_proc(11, "python3", 7, cgroup="0::/system.slice/stcollector-rollup.service\n")
        self.add_proc(12, "python3", 3, cgroup="0::/user.slice/session-1.scope\n")
        self.add_proc(13, "smartthings", 9)
        procs = self.reader().processes(0.0)
        self.assertEqual(sorted(procs.values()), [("python3", 3), ("python3 (집계)", 7), ("smartthings", 9)])

    def test_never_reads_command_lines(self):
        self.add_proc(20, "tool", 1, cmdline="tool --password=SECRETVALUE")
        procs = self.reader().processes(0.0)
        self.assertNotIn("SECRETVALUE", repr(procs))

    def test_missing_files_give_none(self):
        s = SystemReader(self.tmp / "a", self.tmp / "b", throttled_path=self.tmp / "c", disk_path=self.tmp).sample(T0)
        self.assertIsNone(s.temp_c)
        self.assertIsNone(s.undervoltage)


class MonitorTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store = SysmonStore(self.tmp / "s.db")
        self.monitor = Monitor(self.store, SysmonSettings(flush_s=600), HZ, {"undervoltage": True, "throttle_bits": False})

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def count(self, table):
        return self.store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def events(self):
        return self.store.conn.execute(
            "SELECT kind, start_ms, end_ms, seconds, interrupted, procs_json FROM events ORDER BY id").fetchall()

    def test_event_keeps_preceding_processes(self):
        ts = T0
        ticks = 0
        for i in range(13):
            ticks += 200
            self.monitor.step(sample(ts), {("1", 1): ("smartthings", ticks), ("2", 1): ("python3 (수집)", i)})
            ts += STEP
        self.monitor.step(sample(ts, undervoltage=True), {("1", 1): ("smartthings", ticks + 200), ("2", 1): ("python3 (수집)", 13)})
        rows = self.events()
        self.assertEqual(rows[0][0], "undervoltage")
        self.assertIsNone(rows[0][2])
        procs = json.loads(rows[0][5])
        self.assertEqual(procs[0]["name"], "smartthings")
        self.assertEqual(procs[0]["cpu_s"], 24.0)  # 60초 창 12칸 × 2초
        self.assertEqual(procs[0]["pct"], 40.0)

    def test_new_process_counts_all_ticks_first_seen(self):
        self.monitor.step(sample(T0), {})
        self.monitor.step(sample(T0 + STEP), {("9", 1): ("smartthings", 300)})
        self.assertEqual(self.monitor.top_processes()[0]["cpu_s"], 3.0)

    def test_pid_reuse_is_a_new_process(self):
        self.monitor.step(sample(T0), {("9", 1): ("a", 1000)})
        self.monitor.step(sample(T0 + STEP), {("9", 2): ("b", 5)})
        self.assertEqual(self.monitor.top_processes()[0], {"name": "b", "cpu_s": 0.05, "pct": 1.0})

    def test_short_dip_stays_one_event(self):
        ts = T0
        for flag in (True, True, False, False, True, True):
            self.monitor.step(sample(ts, undervoltage=flag), {})
            ts += STEP
        self.assertEqual(self.count("events"), 1)

    def test_event_ends_after_clear_time(self):
        ts = T0
        for _ in range(3):
            self.monitor.step(sample(ts, undervoltage=True), {})
            ts += STEP
        last = ts - STEP
        for _ in range(7):
            self.monitor.step(sample(ts), {})
            ts += STEP
        row = self.events()[0]
        self.assertEqual((row[2], row[3]), (last, 15))

    def test_overheat_uses_threshold(self):
        self.monitor.step(sample(T0, temp_c=79.9), {})
        self.assertEqual(self.count("events"), 0)
        self.monitor.step(sample(T0 + STEP, temp_c=80.0), {})
        self.assertEqual(self.events()[0][0], "overheat")

    def test_minutes_are_batched_until_flush(self):
        ts = T0
        for _ in range(12 * 9):  # 9분
            self.monitor.step(sample(ts), {})
            ts += STEP
        self.assertEqual(self.count("minutes"), 0)
        for _ in range(12 * 2):
            self.monitor.step(sample(ts), {})
            ts += STEP
        self.assertEqual(self.count("minutes"), 10)
        self.assertEqual(len(self.monitor.pending), 0)

    def test_event_flushes_pending_minutes_immediately(self):
        ts = T0
        for _ in range(13):
            self.monitor.step(sample(ts), {})
            ts += STEP
        self.monitor.step(sample(ts, undervoltage=True), {})
        self.assertEqual(self.count("minutes"), 1)

    def test_minute_summary_values(self):
        for i, t in enumerate([50.0, 54.0, 52.0]):
            self.monitor.step(Sample(ts_ms=T0 + i * STEP, temp_c=t, freq_mhz=900.0, load1=1.0, mem_avail_mb=500 + i, undervoltage=i == 1), {})
        self.monitor.step(sample(T0 + 60_000), {})
        self.monitor.flush()
        r = self.store.conn.execute("SELECT temp_avg, temp_min, temp_max, mem_avail_mb, undervolt_s, samples FROM minutes").fetchone()
        self.assertEqual(tuple(r), (52.0, 50.0, 54.0, 500, 5, 3))

    def test_restart_closes_unfinished_event(self):
        self.monitor.step(sample(T0, undervoltage=True), {})
        next_run = Monitor(self.store, SysmonSettings(), HZ)
        next_run.begin("boot-1", T0 + 60_000, 30)
        row = self.events()[0]
        self.assertEqual(row[4], 1)
        self.assertIsNotNone(row[2])

    def test_boot_recorded_once_per_boot_id(self):
        self.monitor.begin("boot-1", T0, 30)
        self.monitor.begin("boot-1", T0 + 1000, 31)
        self.monitor.begin("boot-2", T0 + 2000, 5)
        rows = [r for r in self.events() if r[0] == "boot"]
        self.assertEqual([r[1] for r in rows], [T0 - 30_000, T0 + 2000 - 5000])

    def test_retention(self):
        s = SysmonSettings(minute_retention_days=1, event_retention_days=2)
        store = self.store
        store.add_minutes([dict(ts_ms=T0 - 2 * 86_400_000, temp_avg=1, temp_min=1, temp_max=1, freq_avg=1, load_avg=1,
                                mem_avail_mb=1, disk_free_gb=1, undervolt_s=0, overheat_s=0, throttle_s=0, samples=1)])
        store.start_event("boot", T0 - 3 * 86_400_000, None, [])
        store.apply_retention(T0, s.minute_retention_days, s.event_retention_days)
        self.assertEqual(self.count("minutes"), 0)
        self.assertEqual(self.count("events"), 0)

    def test_stop_saves_buffered_data(self):
        self.monitor.step(sample(T0, undervoltage=True), {})
        self.monitor.stop()
        self.assertEqual(self.count("minutes"), 1)
        self.assertIsNotNone(self.events()[0][2])

    def test_snapshot_includes_unsaved_minutes(self):
        self.monitor.step(sample(T0), {})
        snap = self.monitor.snapshot(sample(T0))
        self.assertEqual(snap["pending"][0]["ts_ms"], T0)


class RunLoopTests(TempDirMixin, unittest.TestCase):
    class Reader:
        capabilities = {"undervoltage": True, "throttle_bits": False}

        def __init__(self, stop, total):
            self.stop, self.total, self.calls = stop, total, 0

        def sample(self, ts_ms):
            self.calls += 1
            if self.calls >= self.total:
                self.stop.set()
            return Sample(ts_ms=ts_ms, temp_c=50.0, freq_mhz=900.0, load1=0.5, uptime_s=100, undervoltage=self.calls >= 3)

        def boot_id(self):
            return "boot-x"

        def processes(self, now):
            return {("1", 1): ("smartthings", self.calls * 100)}

        def disk(self):
            return (20.0, 29.0)

    def test_loop_writes_current_and_saves_on_stop(self):
        stop = threading.Event()
        stop.wait = lambda timeout=None: None  # 주기 대기 생략
        ticks = iter(range(T0, T0 + 1000 * STEP, STEP))
        current = self.tmp / "current.json"
        settings = SysmonSettings(interval_s=1, runtime_file=str(current))
        db = self.tmp / "sensors.db"
        with mock.patch("stcollector.sysmon._clock_ticks", return_value=HZ):
            code = run_monitor(db, settings, stop, reader=self.Reader(stop, 5), clock=lambda: next(ticks) / 1000)
        self.assertEqual(code, 0)
        snap = json.loads(current.read_text("utf-8"))
        self.assertEqual(snap["active"], ["undervoltage"])
        self.assertEqual(snap["disk_free_gb"], 20.0)
        store = SysmonStore(sysmon_db_path(db))
        kinds = [r[0] for r in store.conn.execute("SELECT kind FROM events ORDER BY id")]
        ended = store.conn.execute("SELECT COUNT(*) FROM events WHERE end_ms IS NULL").fetchone()[0]
        minutes = store.conn.execute("SELECT COUNT(*) FROM minutes").fetchone()[0]
        store.close()
        self.assertEqual(kinds, ["boot", "undervoltage"])
        self.assertEqual(ended, 0)
        self.assertEqual(minutes, 1)


class CommandTests(TempDirMixin, unittest.TestCase):
    def test_sysmon_command_uses_separate_lock_and_settings(self):
        from stcollector.__main__ import main

        config = self.tmp / "c.toml"
        db = (self.tmp / "s.db").as_posix()
        config.write_text(f'[paths]\ndb = "{db}"\ntoken = "{(self.tmp / "t.json").as_posix()}"\n\n[sysmon]\ninterval_s = 7\n', "utf-8")
        with mock.patch("stcollector.sysmon.run_monitor", return_value=0) as run, mock.patch("signal.signal"):
            self.assertEqual(main(["--config", str(config), "sysmon"]), 0)
        self.assertEqual(run.call_args.args[1].interval_s, 7)


class SettingsTests(TempDirMixin, unittest.TestCase):
    def write(self, text):
        p = self.tmp / "c.toml"
        p.write_text(text, "utf-8")
        return p

    def test_defaults_when_section_missing(self):
        self.assertEqual(load_sysmon_settings(self.write("")).interval_s, 5)

    def test_rejects_bad_values(self):
        for text in ("[sysmon]\ninterval_s = 0\n", "[sysmon]\nflush_s = 1\n", "[sysmon]\noverheat_c = 5\n"):
            with self.assertRaises(ConfigError):
                load_sysmon_settings(self.write(text))

    def test_example_config_loads(self):
        s = load_sysmon_settings(ROOT / "config.example.toml")
        self.assertEqual((s.interval_s, s.flush_s), (5, 600))

    def test_db_is_separate_from_collector_db(self):
        self.assertNotEqual(sysmon_db_path("/var/lib/stcollector/sensors.db").name, "sensors.db")


class ServiceFileTests(unittest.TestCase):
    def test_sandbox(self):
        text = (ROOT / "deploy/systemd/stcollector-sysmon.service").read_text("utf-8")
        for line in ("ProtectProc=default", "PrivateNetwork=true", "IPAddressDeny=any", "NoNewPrivileges=true",
                     "ProtectSystem=strict", "MemoryDenyWriteExecute=true", "RestrictAddressFamilies=AF_UNIX",
                     "StateDirectory=stcollector/sysmon"):
            self.assertIn(line, text)
        self.assertNotIn("ProtectProc=invisible", text.splitlines())
        self.assertNotIn("CAP_", text)

    def test_web_and_rollup_keep_their_restrictions(self):
        for name in ("stcollector-web.service", "stcollector-rollup.service"):
            text = (ROOT / "deploy/systemd" / name).read_text("utf-8")
            self.assertIn("ProtectProc=invisible", text)
            self.assertIn("MemoryDenyWriteExecute=true", text)


class SystemApiTests(TempDirMixin, unittest.TestCase):
    def start(self, with_data=True):
        settings = WebSettings("127.0.0.1", 0, hash_password("x-test-only"), (ipaddress.ip_network("192.168.0.0/24"),), 30)
        db_path = self.tmp / "sensors.db"
        current = self.tmp / "current.json"
        if with_data:
            store = SysmonStore(sysmon_db_path(db_path))
            now = int(__import__("time").time() * 1000)
            minute = now - now % 60_000
            store.add_minutes([dict(ts_ms=minute - 120_000, temp_avg=55.0, temp_min=54.0, temp_max=57.0, freq_avg=900.0, load_avg=1.0,
                                    mem_avail_mb=500, disk_free_gb=20.0, undervolt_s=10, overheat_s=0, throttle_s=0, samples=12)])
            store.end_event(store.start_event("undervoltage", minute - 120_000, Sample(ts_ms=minute, temp_c=55.0), [{"name": "smartthings", "cpu_s": 3.0, "pct": 5.0}]), minute - 100_000, 10)
            store.close()
            write_runtime(current, {"ts_ms": now, "temp_c": 51.0, "freq_mhz": 900.0, "load1": 0.5, "active": ["undervoltage"],
                                    "top": [], "undervoltage": True, "capabilities": {"undervoltage": True, "throttle_bits": False},
                                    "pending": [dict(ts_ms=minute, temp_avg=60.0, temp_min=60.0, temp_max=60.0, freq_avg=900.0, load_avg=1.0, undervolt_s=0, overheat_s=0)]})
        app = server.App(db_path, settings, sysmon_current=current)
        app.sessions.valid = lambda token: token == "ok"
        self.srv = server.Server(("127.0.0.1", 0), app)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def get(self, path, cookie="st_session=ok"):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request("GET", path, headers={"Cookie": cookie} if cookie else {})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    def test_requires_login(self):
        self.start()
        self.assertEqual(self.get("/api/system", cookie=None)[0], 401)

    def test_returns_series_events_and_unsaved_minute(self):
        self.start()
        status, body = self.get("/api/system")
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertTrue(d["available"])
        self.assertEqual(d["current"]["temp_c"], 51.0)
        self.assertFalse(d["current"]["stale"])
        self.assertNotIn("pending", d["current"])
        self.assertEqual(len(d["series"]["buckets"]), 288)
        self.assertEqual(sum(d["series"]["undervolt_s"]), 10)
        self.assertIn(60.0, d["series"]["temp_max"])
        self.assertEqual(d["events"][0]["procs"][0]["name"], "smartthings")

    def test_active_warning_appears_in_banner(self):
        self.start()
        alerts = json.loads(self.get("/api/meta")[1])["alerts"]
        self.assertTrue(any("전압 부족" in a["text"] for a in alerts))

    def request(self, path, cookie=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request("GET", path, headers={"Cookie": cookie} if cookie else {})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r, body

    def test_version_is_returned_after_login(self):
        self.start()
        response, body = self.request("/api/meta", "st_session=ok")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body)["version"], __version__)
        self.assertNotIn(__version__, response.getheader("Server", ""))

    def test_version_is_hidden_before_login(self):
        self.start()
        for path, status in (("/api/meta", 401), ("/login", 200), ("/static/style.css", 200)):
            response, body = self.request(path)
            self.assertEqual(response.status, status, path)
            self.assertNotIn(__version__.encode(), body, path)
            self.assertNotIn(__version__, response.getheader("Server", ""), path)

    def test_page_has_version_footer(self):
        index = (ROOT / "stcollector/web/static/index.html").read_text("utf-8")
        script = (ROOT / "stcollector/web/static/app.js").read_text("utf-8")
        self.assertIn('id="footer"', index)
        self.assertIn("meta.version", script)
        self.assertNotIn(__version__, index)

    def test_without_monitor(self):
        self.start(with_data=False)
        status, body = self.get("/api/system")
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertFalse(d["available"])
        self.assertEqual(d["events"], [])


if __name__ == "__main__":
    unittest.main()
