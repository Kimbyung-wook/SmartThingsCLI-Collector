"""SmartThings 서버가 위치 이력 조회에 HTTP 500 을 이어서 돌려줄 때의 수집 동작과 대시보드 경고."""

import threading
import unittest
from unittest import mock

from stcollector import cli_client
from stcollector.cli_client import SmartThingsCliClient
from stcollector.collector import HISTORY_OVERLAP_MS, Collector
from stcollector.db import Database
from stcollector.transport import HttpError
from stcollector.web import api

from .helpers import TempDirMixin
from .test_cli_client import (
    CURSOR_KEY, DEVICES, LOC, NOW, NOW_MS, ROOMS, CliTestBase, FakeCli, ev, option,
)

# RPi 로그에서 본 형식 (requestId 등은 가짜)
SERVER_500 = (1, "", 'Request to API failed with status 500\nResponse:\n {\n "requestId": "fake-id",\n'
                     ' "error": {\n "code": "UnexpectedError",\n'
                     ' "message": "A non-recoverable error condition occurred." } }')

STATUS_DEVICES = [
    {"deviceId": "t1", "components": [{"id": "main", "capabilities": [
        {"id": "temperatureMeasurement",
         "status": {"temperature": {"value": 21.0, "unit": "C", "timestamp": "2023-11-14T22:10:00Z"}}}]}]},
    {"deviceId": "m1", "components": [{"id": "main", "capabilities": [
        {"id": "motionSensor", "status": {"motion": {"value": "active", "timestamp": "2023-11-14T22:11:00Z"}}}]}]},
]


class OutageCli(FakeCli):
    """`devices` 는 목록 조회와 `--status` 조회를 나눠 답한다."""

    def __init__(self, status_response=STATUS_DEVICES):
        super().__init__()
        self.status_response = status_response

    def __call__(self, argv, env, timeout_s):
        if argv[1] == "devices":
            self.calls.append({"argv": list(argv), "env": dict(env), "timeout": timeout_s})
            resp = self.status_response if "--status" in argv else DEVICES
            if isinstance(resp, tuple):
                return resp
            return 0, cli_client.json.dumps(resp), ""
        return super().__call__(argv, env, timeout_s)

    def status_calls(self):
        return [c for c in self.named("devices") if "--status" in c]


class OutageTestBase(CliTestBase):
    def setUp(self):
        super().setUp()
        self.fake = OutageCli()
        self.fake.add("locations:rooms", ROOMS)
        self.db = Database(self.tmp / "sensors.db")
        self.now = NOW

    def tearDown(self):
        self.db.close()
        super().tearDown()

    def advance(self, seconds):
        self.now += seconds

    def collector(self, **overrides):
        cfg = self.cfg(mode="history", **overrides)
        client = SmartThingsCliClient(cfg, runner=self.fake, sleep=self.advance)
        return Collector(cfg, client, self.db, clock=lambda: self.now)


class RunLoopOutageTest(OutageTestBase):
    def run_until_recovered(self, c):
        """가짜 시계로 루프를 돌리고, 이력이 처음 저장된 뒤 첫 대기에서 멈춘다. 대기 시간 목록을 돌려준다."""
        stop = threading.Event()
        waits = []

        def fake_wait(timeout=None):
            waits.append(timeout)
            self.advance(timeout)
            stored = self.db.conn.execute(
                "SELECT COUNT(*) FROM readings WHERE source = 'history'").fetchone()[0]
            if stored or len(waits) > 50:
                stop.set()
            return stop.is_set()

        with mock.patch.object(stop, "wait", fake_wait), \
                self.assertLogs("stcollector", "INFO") as logs:
            c.run(stop)
        return waits, logs

    def test_retry_never_waits_longer_than_collection_interval(self):
        # 이력 조회 5번(CLI 재시도 포함 10회) 실패 뒤 회복
        self.fake.add("locations:history", *([SERVER_500] * 10), [ev(NOW_MS - 1000)])
        c = self.collector()
        waits, logs = self.run_until_recovered(c)

        failure_waits = waits[:-1]
        self.assertEqual(failure_waits, [30.0, 60.0, 120.0, 240.0, 300.0])
        self.assertLessEqual(max(failure_waits), c.cfg.history_interval_s)
        self.assertEqual(len(self.fake.named("locations:history")), 11)
        # 회복 뒤 상태가 정리되고 이력이 저장된다
        self.assertEqual(self.db.get_meta(CURSOR_KEY), str(NOW_MS - 1000))
        self.assertIsNone(self.db.get_meta("history_fail_since_ms"))
        self.assertIsNone(self.db.get_meta("history_failures"))
        self.assertEqual(self.db.get_meta("auth_state"), "ok")
        self.assertTrue(any("회복" in r.getMessage() for r in logs.records))
        # 서버 오류는 스택 없이 한 줄로 기록된다
        errors = [r for r in logs.records if r.levelname == "ERROR"]
        self.assertEqual(len(errors), 5)
        self.assertTrue(all(r.exc_info is None and "HTTP 500" in r.getMessage() for r in errors))

    def test_current_values_are_kept_while_history_fails(self):
        self.fake.add("locations:history", *([SERVER_500] * 10), [ev(NOW_MS - 1000)])
        c = self.collector()
        self.run_until_recovered(c)
        # 첫 실패는 넘기고, 두 번째 실패부터 수집 주기(300초)에 한 번만 상태를 읽는다.
        # 실패 시각: +30, +90(임시 수집), +180, +330, +600(임시 수집)
        self.assertEqual(len(self.fake.status_calls()), 2)
        sources = dict(self.db.conn.execute(
            "SELECT source, COUNT(*) FROM readings GROUP BY source").fetchall())
        self.assertEqual(sources.get("status"), 2)  # 현재값은 같은 시각이라 한 번만 저장
        self.assertEqual(sources.get("history"), 1)
        self.assertIsNotNone(self.db.get_meta("status_fallback_ms"))

    def test_failed_range_is_read_again_after_recovery(self):
        c = self.collector()
        self.db.set_meta(CURSOR_KEY, NOW_MS - 60_000)
        for d in ("t1", "m1"):
            self.db.set_cursor(d, NOW_MS - 60_000)
        self.fake.add("locations:history", *([SERVER_500] * 4), [ev(NOW_MS + 500_000)])
        self.run_until_recovered(c)
        expected = cli_client._to_iso(NOW_MS - 60_000 - HISTORY_OVERLAP_MS)
        afters = {option(a, "--after") for a in self.fake.named("locations:history")}
        self.assertEqual(afters, {expected})


class FallbackTest(OutageTestBase):
    def test_single_failure_does_not_trigger_status_call(self):
        self.fake.add("locations:history", SERVER_500, SERVER_500, [ev(NOW_MS - 1000)])
        c = self.collector()
        c.refresh_devices()
        with self.assertRaises(HttpError):
            c.poll_history_or_fallback()
        self.assertEqual(self.fake.status_calls(), [])
        self.assertEqual(self.db.get_meta("history_failures"), "1")
        self.assertEqual(self.db.get_meta("history_fail_since_ms"), str(int(self.now * 1000)))
        with self.assertLogs("stcollector.collector", "INFO"):
            self.assertEqual(c.poll_history_or_fallback(), 1)
        self.assertIsNone(self.db.get_meta("history_fail_since_ms"))

    def test_fail_since_keeps_first_failure_time(self):
        self.fake.add("locations:history", SERVER_500)
        c = self.collector()
        c.refresh_devices()
        first = int(self.now * 1000) + int(cli_client.RETRY_DELAY_S * 1000)  # CLI 재시도 뒤 기록
        for _ in range(3):
            with self.assertRaises(HttpError), self.assertLogs("stcollector", "WARNING"):
                c.poll_history_or_fallback()
        self.assertEqual(self.db.get_meta("history_fail_since_ms"), str(first))
        self.assertEqual(self.db.get_meta("history_failures"), "3")

    def test_failing_fallback_keeps_original_error(self):
        self.fake = OutageCli(status_response=SERVER_500)
        self.fake.add("locations:rooms", ROOMS)
        self.fake.add("locations:history", SERVER_500)
        c = self.collector()
        c.refresh_devices()
        last_ok = self.db.get_meta("last_ok_ms")
        with self.assertRaises(HttpError), self.assertLogs("stcollector", "WARNING"):
            c.poll_history_or_fallback()
        with self.assertRaises(HttpError) as caught, self.assertLogs("stcollector", "WARNING") as logs:
            c.poll_history_or_fallback()
        self.assertIn("locations:history", str(caught.exception))
        self.assertTrue(any("임시 수집도 실패" in r.getMessage() for r in logs.records))
        self.assertIsNone(self.db.get_meta("status_fallback_ms"))
        self.assertEqual(self.db.get_meta("last_ok_ms"), last_ok)


class OutageAlertTest(TempDirMixin, unittest.TestCase):
    NOW_MS = 1_800_000_000_000
    DISK = {"total": 1, "used": 0, "free": 10**12, "db_bytes": 0}

    def setUp(self):
        super().setUp()
        self.db = Database(self.tmp / "s.db")
        self.db.set_meta("last_ok_ms", self.NOW_MS - 60_000)

    def tearDown(self):
        self.db.close()
        super().tearDown()

    def alerts(self):
        ctx = api.Context(self.db.conn, self.NOW_MS, self.tmp / "s.db")
        return api.alerts(ctx, self.DISK, [])

    def test_short_failure_is_not_shown(self):
        self.db.set_meta("history_fail_since_ms", self.NOW_MS - 5 * 60_000)
        self.assertEqual(self.alerts(), [])

    def test_long_failure_shows_warning_with_fallback_note(self):
        self.db.set_meta("history_fail_since_ms", self.NOW_MS - 25 * 60_000)
        self.db.set_meta("status_fallback_ms", self.NOW_MS - 2 * 60_000)
        (alert,) = self.alerts()
        self.assertEqual(alert["level"], "warning")
        self.assertIn("25분째", alert["text"])
        self.assertIn("현재값만 임시로", alert["text"])

    def test_old_fallback_time_is_not_mentioned(self):
        self.db.set_meta("history_fail_since_ms", self.NOW_MS - 25 * 60_000)
        self.db.set_meta("status_fallback_ms", self.NOW_MS - 90 * 60_000)
        (alert,) = self.alerts()
        self.assertNotIn("임시", alert["text"])

    def test_cleared_failure_is_not_shown(self):
        self.db.set_meta("history_fail_since_ms", None)
        self.assertEqual(self.alerts(), [])


if __name__ == "__main__":
    unittest.main()
