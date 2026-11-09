"""CLI 백엔드: 허용 목록, 실행 안전장치, 오류 처리, 수집 흐름, 명령행 분기."""

import contextlib
import inspect
import io
import json
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from stcollector import cli_client
from stcollector.__main__ import main
from stcollector.auth import ReauthRequired
from stcollector.cli_client import (
    ALLOWED_COMMANDS, CREDENTIALS_RELATIVE, CliCommandNotAllowed, CliError,
    SmartThingsCliClient, build_argv, run_cli, sanitize,
)
from stcollector.collector import DAY_MS, HISTORY_OVERLAP_MS, Collector
from stcollector.config import ConfigError, load_config
from stcollector.db import Database
from stcollector.transport import HttpError

from .helpers import TempDirMixin, make_config

NOW = 1_700_000_000.0
NOW_MS = int(NOW * 1000)
LOC = "loc-1"
CURSOR_KEY = f"history_cursor:{LOC}"

DEVICES = [
    {"deviceId": "t1", "label": "가짜 온습도", "name": "fake-th", "roomId": "r1",
     "components": [{"id": "main", "capabilities": [
         {"id": "temperatureMeasurement"}, {"id": "relativeHumidityMeasurement"}]}]},
    {"deviceId": "m1", "label": "가짜 모션", "name": "fake-motion", "roomId": "r1",
     "components": [{"id": "main", "capabilities": [{"id": "motionSensor"}]}]},
    {"deviceId": "x1", "label": "가짜 리모컨", "roomId": "r1",
     "components": [{"id": "main", "capabilities": [{"id": "refresh"}]}]},
]
ROOMS = [{"roomId": "r1", "name": "가짜 방", "locationId": LOC}]


def ev(epoch, device="t1", cap="temperatureMeasurement", attr="temperature", value=22.5, unit="C"):
    return {"deviceId": device, "deviceName": "fake", "component": "main", "capability": cap,
            "attribute": attr, "value": value, "unit": unit, "epoch": epoch,
            "time": "2023-11-14T22:13:20.000Z", "hash": epoch % 997}


class FakeCli:
    """명령 이름별로 응답을 돌려주는 가짜 실행 함수. 응답은 JSON 값, 튜플, 예외."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def add(self, command, *responses):
        self.routes.setdefault(command, []).extend(responses)

    def __call__(self, argv, env, timeout_s):
        self.calls.append({"argv": list(argv), "env": dict(env), "timeout": timeout_s})
        queue = self.routes.get(argv[1])
        if not queue:
            raise AssertionError(f"예상하지 못한 CLI 호출: {argv}")
        resp = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(resp, BaseException):
            raise resp
        if isinstance(resp, tuple):
            return resp
        return 0, json.dumps(resp), ""

    def named(self, command):
        return [c["argv"] for c in self.calls if c["argv"][1] == command]


def option(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


class CliTestBase(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeCli()
        self.home = self.tmp / "cli-home"
        self.credentials = self.home / CREDENTIALS_RELATIVE
        self.credentials.parent.mkdir(parents=True)
        self.credentials.write_text("{}", "utf-8")
        self.sleeps = []

    def cfg(self, **overrides):
        values = dict(backend="cli", cli_path="/fake/smartthings", cli_home=str(self.home),
                      cli_timeout_s=7)
        values.update(overrides)
        return make_config(self.tmp, **values)

    def client(self, **overrides):
        return SmartThingsCliClient(self.cfg(**overrides), runner=self.fake, sleep=self.sleeps.append)


class AllowListTest(unittest.TestCase):
    def test_only_read_commands_are_allowed(self):
        self.assertEqual(
            set(ALLOWED_COMMANDS),
            {"locations", "locations:rooms", "locations:history", "devices",
             "devices:status", "devices:history"},
        )

    def test_control_and_account_commands_are_rejected(self):
        for command in ("devices:commands", "devices:delete", "devices:update", "devices:rename",
                        "locations:delete", "locations:create", "locations:update",
                        "locations:rooms:delete", "rules:create", "scenes:execute",
                        "login", "logout", "config", "smartapps:create", "DEVICES:COMMANDS", ""):
            with self.subTest(command=command), self.assertRaises(CliCommandNotAllowed):
                build_argv("/fake/cli", command)

    def test_extra_positional_cannot_reach_another_command(self):
        for command, positional in (("devices", ("commands",)), ("locations", ("delete",)),
                                    ("devices:status", ("a", "b")), ("devices:status", ())):
            with self.subTest(command=command), self.assertRaises(CliCommandNotAllowed):
                build_argv("/fake/cli", command, positional)

    def test_arguments_cannot_inject_flags(self):
        cases = [
            ("devices:status", ("--help",), None),
            ("devices:status", ("id;reboot",), None),
            ("devices:status", ("id\n",), None),
            ("devices", (), {"--profile": "x"}),
            ("devices:status", ("id",), {"--limit": 1}),
            ("locations:history", ("id",), {"--after": "--json"}),
            ("locations:history", ("id",), {"--after": "a b"}),
        ]
        for command, positional, options in cases:
            with self.subTest(positional=positional, options=options), self.assertRaises(CliCommandNotAllowed):
                build_argv("/fake/cli", command, positional, options)

    def test_json_is_always_requested(self):
        self.assertEqual(build_argv("/fake/cli", "locations"), ["/fake/cli", "locations", "--json"])

    def test_blocked_command_never_reaches_runner(self):
        fake = FakeCli()
        with TempHome() as home:
            client = SmartThingsCliClient(make_cfg_for(home), runner=fake)
            with self.assertRaises(CliCommandNotAllowed):
                client._run("devices:commands", ("d1",))
        self.assertEqual(fake.calls, [])

    def test_client_exposes_no_write_method(self):
        names = [n for n, _ in inspect.getmembers(SmartThingsCliClient, inspect.isfunction)]
        for name in names:
            self.assertNotRegex(name, r"(?i)command|execute|delete|update|create|rename|logout")

    def test_source_never_enables_shell(self):
        source = inspect.getsource(cli_client)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("os.system", source)


class TempHome:
    """AllowListTest 처럼 믹스인이 없는 경우의 임시 CLI HOME."""

    def __enter__(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        home = Path(self._tmp.name)
        path = home / CREDENTIALS_RELATIVE
        path.parent.mkdir(parents=True)
        path.write_text("{}", "utf-8")
        return home

    def __exit__(self, *exc):
        self._tmp.cleanup()


def make_cfg_for(home):
    return make_config(home, backend="cli", cli_path="/fake/smartthings", cli_home=str(home))


class ClientTest(CliTestBase):
    def test_env_is_minimal_and_home_is_cli_home(self):
        self.fake.add("locations", [])
        with mock.patch.dict("os.environ", {"LEAK_ME": "x", "SMARTTHINGS_TOKEN": "y"}):
            self.client().locations()
        call = self.fake.calls[0]
        self.assertEqual(set(call["env"]), {"HOME", "PATH", "LANG", "NO_COLOR"})
        self.assertEqual(call["env"]["HOME"], str(self.home))
        self.assertEqual(call["timeout"], 7)

    def test_endpoints_build_expected_arguments(self):
        self.fake.add("locations", [{"locationId": LOC}])
        self.fake.add("locations:rooms", ROOMS)
        self.fake.add("devices", DEVICES)
        self.fake.add("devices:status", {"components": {}})
        c = self.client()
        self.assertEqual(c.locations(), [{"locationId": LOC}])
        self.assertEqual(c.rooms(LOC), ROOMS)
        self.assertEqual(c.devices(LOC), DEVICES)
        self.assertEqual(c.device_status("t1"), {"components": {}})
        self.assertEqual(self.fake.calls[1]["argv"],
                         ["/fake/smartthings", "locations:rooms", "--json", "--location", LOC])
        self.assertEqual(self.fake.calls[3]["argv"],
                         ["/fake/smartthings", "devices:status", "t1", "--json"])

    def test_rejects_non_list_output(self):
        self.fake.add("locations", {"items": [{"locationId": LOC}]})
        with self.assertRaises(CliError):
            self.client().locations()

    def test_after_uses_utc_iso_with_milliseconds(self):
        self.fake.add("locations:history", [])
        list(self.client().location_history_pages(LOC, limit=5, after_ms=1_700_000_000_123))
        self.assertEqual(option(self.fake.calls[0]["argv"], "--after"), "2023-11-14T22:13:20.123Z")

    def test_short_page_makes_single_call(self):
        self.fake.add("locations:history", [ev(5000), ev(4000)])
        pages = list(self.client().location_history_pages(LOC, limit=3, after_ms=1000))
        self.assertEqual(len(pages), 1)
        self.assertEqual(len(self.fake.calls), 1)

    def test_after_walks_forward_one_ms_before_newest(self):
        # 실제 CLI처럼 페이지 안은 최신순이고, 페이지는 오래된 구간부터 온다.
        self.fake.add("locations:history",
                      [ev(4000), ev(3000), ev(2000)], [ev(7000), ev(6000), ev(5000)], [ev(8000)])
        pages = list(self.client().location_history_pages(LOC, limit=3, after_ms=1000))
        self.assertEqual(len(pages), 3)
        argvs = self.fake.named("locations:history")
        self.assertEqual(
            [option(a, "--after") for a in argvs],
            ["1970-01-01T00:00:01.000Z", "1970-01-01T00:00:03.999Z", "1970-01-01T00:00:06.999Z"],
        )
        self.assertTrue(all("--before" not in a for a in argvs))

    def test_after_walk_always_progresses(self):
        self.fake.add("locations:history", [ev(2000), ev(2000)], [ev(2000), ev(2000)], [])
        list(self.client().location_history_pages(LOC, limit=2, after_ms=2000))
        afters = [option(a, "--after") for a in self.fake.named("locations:history")]
        self.assertEqual(
            afters,
            ["1970-01-01T00:00:02.000Z", "1970-01-01T00:00:02.001Z", "1970-01-01T00:00:02.002Z"],
        )

    def test_limit_is_capped_at_cli_page_size(self):
        # limit 200 이어도 CLI는 100건만 주므로, 100건을 '짧은 페이지'로 오해하면 안 된다.
        self.fake.add("locations:history", [ev(5000 + i) for i in range(100)], [])
        list(self.client().location_history_pages(LOC, limit=200, after_ms=1000))
        argvs = self.fake.named("locations:history")
        self.assertEqual(option(argvs[0], "--limit"), "100")
        self.assertEqual(len(argvs), 2)

    def test_without_after_walks_back_with_overlapping_before(self):
        self.fake.add("locations:history",
                      [ev(9000), ev(8000), ev(7000)], [ev(7000), ev(6000), ev(5000)], [ev(4000)])
        pages = list(self.client().location_history_pages(LOC, limit=3))
        self.assertEqual(len(pages), 3)
        argvs = self.fake.named("locations:history")
        self.assertIsNone(option(argvs[0], "--before"))
        self.assertEqual(option(argvs[1], "--before"), "1970-01-01T00:00:07.001Z")
        self.assertEqual(option(argvs[2], "--before"), "1970-01-01T00:00:05.001Z")
        self.assertTrue(all("--after" not in a for a in argvs))

    def test_same_millisecond_page_forces_progress_then_stops(self):
        self.fake.add("locations:history", [ev(7000), ev(7000)], [ev(7000), ev(7000)], [ev(7000), ev(7000)])
        with self.assertLogs("stcollector.cli_client", "WARNING"):
            list(self.client().location_history_pages(LOC, limit=2))
        self.assertEqual(len(self.fake.calls), 3)
        self.assertEqual(option(self.fake.calls[2]["argv"], "--before"), "1970-01-01T00:00:07.000Z")

    def test_device_statuses_uses_one_call(self):
        status = {"temperature": {"value": 21.5, "unit": "C", "timestamp": "2026-11-08T05:00:00Z"}}
        self.fake.add("devices", [
            {"deviceId": "t1", "components": [{"id": "main", "capabilities": [
                {"id": "temperatureMeasurement", "status": status}]}]},
            {"deviceId": "x1", "components": [{"id": "main", "capabilities": [
                {"id": "refresh", "status": {}}]}]},
        ])
        result = self.client().device_statuses(LOC, ["t1"])
        self.assertEqual(result, {"t1": {"components": {"main": {"temperatureMeasurement": status}}}})
        self.assertEqual(len(self.fake.calls), 1)
        self.assertIn("--status", self.fake.calls[0]["argv"])


class FailureTest(CliTestBase):
    SECRET = "9f8e7d6c-1234-4abc-8def-0123456789ab"

    def test_missing_credentials_requires_login_without_running(self):
        self.credentials.unlink()
        with self.assertRaises(ReauthRequired):
            self.client().locations()
        self.assertEqual(self.fake.calls, [])

    def test_expired_login_maps_to_reauth(self):
        for stderr in ("Error: Request failed with status code 401",
                       "invalid_grant: refresh token expired",
                       "Please log in to continue",
                       "Unauthorized"):
            with self.subTest(stderr=stderr):
                self.fake.routes["locations"] = [(1, "", stderr)]
                with self.assertRaises(ReauthRequired):
                    self.client().locations()

    def test_timeout_waiting_for_browser_login_maps_to_reauth(self):
        self.fake.add("locations", subprocess.TimeoutExpired(
            ["x"], 7, output="", stderr="open http://localhost:61973/start to log in"))
        with self.assertRaises(ReauthRequired):
            self.client().locations()

    def test_plain_timeout_is_a_cli_error(self):
        self.fake.add("locations", subprocess.TimeoutExpired(["x"], 7))
        with self.assertRaises(CliError) as caught:
            self.client().locations()
        self.assertIn("7", str(caught.exception))

    def test_forbidden_maps_to_http_error_without_retry(self):
        # RPi에서 없는 기기 ID로 확인한 실제 오류 문구 형식 (값은 가짜)
        self.fake.add("locations:history", (1, "", (
            'Request to API failed with status 403\nResponse:\n {\n "requestId": "fake-id",\n'
            ' "error": {\n "code": "ConstraintViolationError",\n "message": "Not authorized" } }')))
        with self.assertRaises(HttpError) as caught:
            list(self.client().location_history_pages(LOC, limit=1))
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(len(self.fake.calls), 1)

    def test_rate_limit_retries_once_then_succeeds(self):
        self.fake.add("locations", (1, "", "status code 429"), [{"locationId": LOC}])
        self.assertEqual(self.client().locations(), [{"locationId": LOC}])
        self.assertEqual(len(self.fake.calls), 2)
        self.assertEqual(self.sleeps, [cli_client.RETRY_DELAY_S])

    def test_rate_limit_gives_up_after_one_retry(self):
        self.fake.add("locations", (1, "", "status code 503"))
        with self.assertRaises(HttpError):
            self.client().locations()
        self.assertEqual(len(self.fake.calls), 2)

    def test_non_json_output_is_error_without_echoing_stdout(self):
        self.fake.add("locations", (0, f"token={self.SECRET}", ""))
        with self.assertRaises(CliError) as caught:
            self.client().locations()
        self.assertNotIn(self.SECRET, str(caught.exception))

    def test_stderr_secret_is_not_in_exception(self):
        leaks = (
            f"Error: boom Authorization: Bearer {self.SECRET}",
            f'Error: {{"access_token": "{self.SECRET}"}}',
            f"Error: refresh_token={self.SECRET}",
            "Error: boom eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcDEF123",
            f"Error: boom {self.SECRET}",
        )
        for stderr in leaks:
            with self.subTest(stderr=stderr):
                self.fake.routes["locations"] = [(1, "", stderr)]
                with self.assertRaises(CliError) as caught:
                    self.client().locations()
                text = str(caught.exception)
                self.assertNotIn(self.SECRET, text)
                self.assertNotIn("eyJhbGci", text)

    def test_secret_past_truncation_point_is_hidden_and_length_capped(self):
        text = "x " * 400 + f"token={self.SECRET}"
        self.assertNotIn(self.SECRET, sanitize(text))
        self.assertLessEqual(len(sanitize(text)), cli_client.MAX_ERROR_CHARS)

    def test_sanitize_keeps_ordinary_message(self):
        self.assertEqual(sanitize("\x1b[31mError:\x1b[0m network down\n"), "Error: network down")

    def test_missing_binary_is_cli_error(self):
        self.fake.add("locations", FileNotFoundError(2, "no such file"))
        with self.assertRaises(CliError):
            self.client().locations()


class RealProcessTest(TempDirMixin, unittest.TestCase):
    def _env(self):
        import os

        env = {"HOME": str(self.tmp), "PATH": os.environ.get("PATH", "")}
        if "SYSTEMROOT" in os.environ:
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        return env

    def test_returns_output_of_child(self):
        code, out, err = run_cli([sys.executable, "-c", "print('[1]')"], self._env(), 30)
        self.assertEqual((code, out.strip(), err), (0, "[1]", ""))

    def test_timeout_kills_child(self):
        started = time.monotonic()
        script = "import sys,time; print('partial',flush=True); time.sleep(60)"
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            run_cli([sys.executable, "-c", script], self._env(), 1.5)
        self.assertLess(time.monotonic() - started, 30)
        self.assertIn("partial", caught.exception.output)


class CollectorCliTest(CliTestBase):
    def setUp(self):
        super().setUp()
        self.db = Database(self.tmp / "sensors.db")
        self.fake.add("locations:rooms", ROOMS)
        self.fake.add("devices", DEVICES)

    def tearDown(self):
        self.db.close()
        super().tearDown()

    def collector(self, **cfg_overrides):
        cfg = self.cfg(**cfg_overrides)
        client = SmartThingsCliClient(cfg, runner=self.fake, sleep=self.sleeps.append)
        c = Collector(cfg, client, self.db, clock=lambda: NOW)
        c.refresh_devices()
        return c

    def history_calls(self):
        return self.fake.named("locations:history")

    def test_refresh_keeps_tracked_devices(self):
        c = self.collector()
        self.assertEqual([d["device_id"] for d in c.devices], ["t1", "m1"])
        self.assertEqual(c.devices[0]["room_name"], "가짜 방")

    def test_first_poll_backfills_with_one_location_call(self):
        self.fake.add("locations:history", [
            ev(NOW_MS - 1000),
            ev(NOW_MS - 2000, device="m1", cap="motionSensor", attr="motion", value="active", unit=None),
            ev(NOW_MS - 3000, device="unknown"),
            ev(NOW_MS - 4000, cap="refresh", attr="x"),
        ])
        c = self.collector()
        self.assertEqual(c.poll_history(), 2)
        self.assertEqual(len(self.fake.calls), 3)  # rooms, devices, 위치 이력 1회
        self.assertEqual(self.fake.named("devices:history"), [])
        since = NOW_MS - 7 * DAY_MS
        self.assertEqual(option(self.history_calls()[0], "--after"), cli_client._to_iso(since))
        self.assertEqual(self.db.get_meta(CURSOR_KEY), str(NOW_MS - 1000))
        self.assertEqual(self.db.get_cursor("t1"), NOW_MS - 1000)
        self.assertEqual(self.db.get_cursor("m1"), NOW_MS - 2000)

    def test_later_poll_uses_ten_minute_overlap_and_dedupes(self):
        self.fake.add("locations:history", [ev(NOW_MS - 1000)])
        c = self.collector()
        self.assertEqual(c.poll_history(), 1)
        self.assertEqual(c.poll_history(), 0)
        expected = cli_client._to_iso(NOW_MS - 1000 - HISTORY_OVERLAP_MS)
        self.assertEqual(option(self.history_calls()[1], "--after"), expected)

    def test_quiet_device_does_not_widen_the_window(self):
        self.fake.add("locations:history", [ev(NOW_MS - 3 * DAY_MS)], [ev(NOW_MS - 1000)], [])
        c = self.collector()
        c.poll_history()
        c.poll_history()
        c.poll_history()
        third = option(self.history_calls()[2], "--after")
        self.assertEqual(third, cli_client._to_iso(NOW_MS - 1000 - HISTORY_OVERLAP_MS))
        self.assertEqual(self.fake.named("devices:history"), [])
        self.assertEqual(self.db.get_cursor("m1"), NOW_MS - 7 * DAY_MS)

    def test_more_events_than_limit_are_all_stored(self):
        page1 = [ev(NOW_MS - i * 1000) for i in range(6, 3, -1)]
        page2 = [ev(NOW_MS - i * 1000) for i in range(4, 1, -1)]
        page3 = [ev(NOW_MS - 1000)]
        self.fake.add("locations:history", list(reversed(page1)), list(reversed(page2)), page3)
        c = self.collector(history_page_limit=3)
        self.assertEqual(c.poll_history(), 6)
        self.assertEqual(len(self.history_calls()), 3)
        self.assertEqual(self.db.get_meta(CURSOR_KEY), str(NOW_MS - 1000))

    def test_failure_mid_walk_resumes_from_last_page(self):
        self.fake.add("locations:history", [ev(NOW_MS - 5000), ev(NOW_MS - 6000)],
                      subprocess.TimeoutExpired(["x"], 7), [])
        c = self.collector(history_page_limit=2)
        with self.assertRaises(CliError):
            c.poll_history()
        self.assertEqual(self.db.get_meta(CURSOR_KEY), str(NOW_MS - 5000))
        self.assertEqual(c.db.conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 2)
        c.poll_history()  # 다음 주기는 기기별 백필 없이 위치 커서에서 잇는다
        expected = cli_client._to_iso(NOW_MS - 5000 - HISTORY_OVERLAP_MS)
        self.assertEqual(option(self.history_calls()[-1], "--after"), expected)
        self.assertEqual(self.fake.named("devices:history"), [])

    def test_page_limit_continues_on_next_poll_without_gap(self):
        self.fake.add("locations:history", [ev(NOW_MS - 7000), ev(NOW_MS - 8000)],
                      [ev(NOW_MS - 5000), ev(NOW_MS - 6000)], [ev(NOW_MS - 1000)])
        c = self.collector(history_page_limit=2, history_backfill_max_pages=2, history_max_pages=1)
        self.assertEqual(c.poll_history(), 4)
        self.assertEqual(len(self.history_calls()), 2)
        self.assertEqual(self.db.get_meta(CURSOR_KEY), str(NOW_MS - 5000))
        self.assertEqual(c.poll_history(), 1)
        self.assertEqual(self.db.get_meta(CURSOR_KEY), str(NOW_MS - 1000))

    def test_stop_during_walk_keeps_position_and_makes_no_more_calls(self):
        c = self.collector(history_page_limit=2)
        c._stop = threading.Event()
        c._stop.set()
        self.fake.add("locations:history", [ev(NOW_MS - 1000), ev(NOW_MS - 2000)])
        c.poll_history()
        self.assertEqual(len(self.history_calls()), 1)
        self.assertEqual(self.db.get_meta(CURSOR_KEY), str(NOW_MS - 1000))

    def test_new_device_gets_its_own_backfill_only(self):
        self.fake.add("locations:history", [ev(NOW_MS - 1000)])
        self.fake.add("devices:history", [
            ev(NOW_MS - 90_000_000, device="m2", cap="motionSensor", attr="motion", value="active", unit=None)])
        c = self.collector()
        c.poll_history()
        c.devices.append({"device_id": "m2", "capabilities": {"motionSensor"}, "label": "x", "name": "x",
                          "room_id": None, "room_name": None, "location_id": LOC})
        self.db.upsert_devices(c.devices)
        self.assertEqual(c.poll_history(), 1)
        self.assertEqual(len(self.fake.named("devices:history")), 1)
        self.assertEqual(len(self.history_calls()), 2)
        c.poll_history()
        self.assertEqual(len(self.fake.named("devices:history")), 1)

    def test_detect_mode_uses_location_probe(self):
        self.fake.add("locations:history", [])
        c = self.collector()
        self.assertEqual(c.detect_mode(), "history")
        self.assertEqual(option(self.history_calls()[0], "--limit"), "1")
        self.assertEqual(self.fake.named("devices:history"), [])

    def test_detect_mode_falls_back_to_status_on_forbidden(self):
        self.fake.add("locations:history", (1, "", "status code 403"))
        c = self.collector()
        self.assertEqual(c.detect_mode(), "status")

    def _status_devices(self):
        status = {"temperature": {"value": 21.0, "unit": "C", "timestamp": "2026-11-08T05:00:00Z"}}
        motion = {"motion": {"value": "active", "timestamp": "2026-11-08T05:00:01Z"}}
        return [
            {"deviceId": "t1", "components": [{"id": "main", "capabilities": [
                {"id": "temperatureMeasurement", "status": status}]}]},
            {"deviceId": "m1", "components": [{"id": "main", "capabilities": [
                {"id": "motionSensor", "status": motion}]}]},
        ]

    def test_status_mode_reads_all_devices_in_one_call_and_waits(self):
        c = self.collector(mode="status")
        self.fake.routes["devices"] = [self._status_devices()]
        self.fake.calls.clear()
        self.assertEqual(c.poll_status_due(), 2)
        self.assertEqual(c.poll_status_due(), 0)
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(c.next_status_due(), NOW + 300)  # 모션 30초여도 history_interval_s 로 제한

    def test_run_loop_records_reauth_state(self):
        self.fake.add("locations:history", (1, "", "Request failed with status code 401"))
        c = self.collector(mode="history")
        stop = threading.Event()
        real_wait = stop.wait

        def stop_on_wait(timeout=None):
            stop.set()
            return real_wait(0)

        with mock.patch.object(stop, "wait", stop_on_wait), self.assertLogs("stcollector.collector", "ERROR"):
            c.run(stop)
        self.assertEqual(self.db.get_meta("auth_state"), "reauth_required")
        self.assertNotIn("token", self.db.get_meta("last_error").lower().replace("토큰", ""))

    def test_run_once_records_healthy_state(self):
        self.fake.add("locations:history", [ev(NOW_MS - 1000)])
        c = self.collector()
        stop = threading.Event()
        original = c.poll_history

        def poll_then_stop():
            n = original()
            stop.set()
            return n

        c.poll_history = poll_then_stop
        c.run(stop)
        self.assertEqual(c.mode, "history")
        self.assertEqual(self.db.get_meta("auth_state"), "ok")


CLI_CONFIG = """
[smartthings]
backend = "cli"
cli_path = "/fake/smartthings"
cli_home = "{home}"
cli_timeout_s = 30
location_id = "loc-1"

[paths]
db = "{db}"
token = "{token}"
"""


class ConfigTest(TempDirMixin, unittest.TestCase):
    def load(self, section):
        path = self.tmp / "c.toml"
        path.write_text(f"[smartthings]\n{section}\n", "utf-8")
        return load_config(path)

    def test_defaults_keep_oauth(self):
        cfg = self.load("")
        self.assertEqual(cfg.backend, "oauth")
        self.assertEqual(cfg.cli_path, "/opt/stcollector-cli/smartthings")
        self.assertEqual(cfg.cli_home, "/var/lib/stcollector/cli")
        self.assertEqual(cfg.cli_timeout_s, 120)

    def test_reads_cli_values(self):
        cfg = self.load('backend = "cli"\ncli_path = "/a/st"\ncli_home = "/b"\ncli_timeout_s = 30')
        self.assertEqual((cfg.backend, cfg.cli_path, cfg.cli_home, cfg.cli_timeout_s),
                         ("cli", "/a/st", "/b", 30))

    def test_rejects_bad_values(self):
        for section in ('backend = "mqtt"', 'cli_path = "relative/st"', 'cli_home = ""',
                        'cli_home = 3', "cli_timeout_s = 0"):
            with self.subTest(section=section), self.assertRaises(ConfigError):
                self.load(section)


class CommandLineTest(CliTestBase):
    def setUp(self):
        super().setUp()
        self.config_path = self.tmp / "config.toml"
        self.config_path.write_text(CLI_CONFIG.format(
            home=self.home.as_posix(), db=(self.tmp / "s.db").as_posix(),
            token=(self.tmp / "t.json").as_posix()), "utf-8")

    def run_cli(self, *args):
        err, out = io.StringIO(), io.StringIO()
        real = SmartThingsCliClient

        def build(cfg):
            return real(cfg, runner=self.fake, sleep=self.sleeps.append)

        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out), \
                mock.patch("stcollector.__main__.SmartThingsCliClient", build):
            code = main(["--config", str(self.config_path), *args])
        return code, out.getvalue(), err.getvalue()

    def test_auth_prints_cli_guidance_without_prompting(self):
        with mock.patch("builtins.input", side_effect=AssertionError("입력을 요구하면 안 됩니다")):
            code, out, _ = self.run_cli("auth")
        self.assertEqual(code, 0)
        self.assertIn("README", out)
        self.assertIn("credentials.json", out)
        self.assertEqual(self.fake.calls, [])

    def test_check_works_without_oauth_credentials(self):
        self.fake.add("locations", [{"name": "가짜 집", "locationId": LOC}])
        self.fake.add("locations:rooms", ROOMS)
        self.fake.add("devices", DEVICES)
        self.fake.add("devices:status", {"components": {"main": {"temperatureMeasurement": {
            "temperature": {"value": 21.0, "unit": "C", "timestamp": "2026-11-08T05:00:00Z"}}}}})
        self.fake.add("locations:history", [ev(NOW_MS - 1000), ev(NOW_MS - 2000)])
        code, out, _ = self.run_cli("check")
        self.assertEqual(code, 0, out)
        self.assertIn("로그인 정보 있음", out)
        self.assertIn("최신순", out)
        self.assertEqual(self.fake.named("devices:history"), [])

    def test_check_reports_missing_login(self):
        self.credentials.unlink()
        code, _, err = self.run_cli("check")
        self.assertEqual(code, 3)
        self.assertIn("인증 필요", err)

    def test_run_starts_collector_without_oauth_credentials(self):
        with mock.patch("stcollector.__main__.Collector") as collector, \
                mock.patch("stcollector.__main__.signal.signal"):
            code, _, _ = self.run_cli("run")
        self.assertEqual(code, 0)
        self.assertIsInstance(collector.call_args.args[1], SmartThingsCliClient)
        collector.return_value.run.assert_called_once()

    def test_oauth_backend_still_requires_credentials(self):
        self.config_path.write_text("[smartthings]\n", "utf-8")
        self.assertEqual(self.run_cli("check")[0], 2)
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
