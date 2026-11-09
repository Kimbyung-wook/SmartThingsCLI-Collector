import json
import unittest

from stcollector.auth import Authenticator, TokenStore
from stcollector.client import SmartThingsClient
from stcollector.collector import (
    DAY_MS, Collector, history_item_to_row, split_value, status_to_rows,
)
from stcollector.db import Database
from stcollector.intervals import interval_report
from stcollector.transport import HttpError

from .helpers import FakeHttp, TempDirMixin, make_config

NOW = 1_700_000_000.0
NOW_MS = int(NOW * 1000)

DEVICES = {
    "items": [
        {"deviceId": "t1", "label": "거실 온습도", "roomId": "r1",
         "components": [{"id": "main", "capabilities": [
             {"id": "temperatureMeasurement"}, {"id": "relativeHumidityMeasurement"}, {"id": "battery"}]}]},
        {"deviceId": "x1", "label": "TV 리모컨", "roomId": "r1",
         "components": [{"id": "main", "capabilities": [{"id": "refresh"}]}]},
    ]
}
ROOMS = {"items": [{"roomId": "r1", "name": "거실"}]}


def ev(epoch, cap="temperatureMeasurement", attr="temperature", value=22.5, unit="C", device="t1"):
    return {"deviceId": device, "component": "main", "capability": cap, "attribute": attr,
            "value": value, "unit": unit, "epoch": epoch, "hash": epoch % 997, "time": "x"}


class ConversionTest(unittest.TestCase):
    def test_split_value(self):
        self.assertEqual(split_value(21), (21.0, None))
        self.assertEqual(split_value("active"), (None, "active"))
        self.assertEqual(split_value(True), (None, "true"))
        self.assertIsNone(split_value(None))
        self.assertEqual(split_value({"a": 1}), (None, json.dumps({"a": 1})))

    def test_history_item_filters_untracked(self):
        attrs = {"temperatureMeasurement": ["temperature"]}
        self.assertIsNotNone(history_item_to_row(ev(1), attrs))
        self.assertIsNone(history_item_to_row(ev(1, cap="refresh", attr="x"), attrs))

    def test_status_to_rows(self):
        status = {"components": {"main": {
            "temperatureMeasurement": {"temperature": {"value": 23.1, "unit": "C",
                                                       "timestamp": "2026-11-08T05:00:00.123Z"}},
            "motionSensor": {"motion": {"value": "inactive", "timestamp": "2026-11-08T05:01:00Z"}},
            "healthCheck": {"checkInterval": {"value": 720}},
        }}}
        rows = status_to_rows("d1", status, {"temperatureMeasurement": ["temperature"],
                                             "motionSensor": ["motion"]})
        self.assertEqual(len(rows), 2)
        temp = [r for r in rows if r[2] == "temperatureMeasurement"][0]
        # 2026-11-08T05:00:00Z = 1794114000 초
        self.assertEqual(temp[4], 1794114000123)
        self.assertEqual(temp[5], 23.1)


class CollectorTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.http = FakeHttp()
        self.http.add("GET", "/v1/locations/loc-1/rooms", ROOMS)
        self.http.add("GET", "/v1/devices", DEVICES)
        self.db = Database(self.tmp / "sensors.db")

    def tearDown(self):
        self.db.close()
        super().tearDown()

    def _collector(self, **cfg_overrides):
        cfg = make_config(self.tmp, **cfg_overrides)
        store = TokenStore(cfg.token_path)
        store.save({"access_token": "A", "refresh_token": "R", "expires_at": NOW + 50000})
        auth = Authenticator(cfg, store, http=self.http, clock=lambda: NOW)
        client = SmartThingsClient(cfg, auth, http=self.http, sleep=lambda s: None)
        return Collector(cfg, client, self.db, clock=lambda: NOW)

    def test_refresh_devices_keeps_only_tracked(self):
        c = self._collector()
        devs = c.refresh_devices()
        self.assertEqual([d["device_id"] for d in devs], ["t1"])
        self.assertEqual(devs[0]["room_name"], "거실")
        self.assertEqual(self.db.device_labels()["t1"], "거실 온습도 (거실)")

    def test_detect_mode_falls_back_to_status(self):
        self.http.add("GET", "/v1/history/devices", HttpError(403, "forbidden"))
        c = self._collector()
        c.refresh_devices()
        self.assertEqual(c.detect_mode(), "status")
        self.assertEqual(self.db.get_meta("mode"), "status")

    def test_history_backfill_stops_at_window_and_dedupes(self):
        old = NOW_MS - 8 * DAY_MS  # 백필 범위(7일) 밖
        page1 = {"items": [ev(NOW_MS - 1000), ev(NOW_MS - 2000, attr="temperature", value=22.4)],
                 "_links": {"next": {"href": "https://api.smartthings.com/v1/history/devices?p=2"}}}
        page2 = {"items": [ev(NOW_MS - 3000, cap="relativeHumidityMeasurement", attr="humidity", value=55, unit="%"),
                           ev(old)],
                 "_links": {"next": {"href": "https://api.smartthings.com/v1/history/devices?p=3"}}}
        self.http.add("GET", "/v1/history/devices", page1, page2)
        c = self._collector()
        c.refresh_devices()
        self.assertEqual(c.poll_history(), 3)
        # page3 요청 없이 멈춰야 한다 (백필 범위 밖 이벤트를 만났으므로)
        history_calls = [x for x in self.http.calls if x["path"] == "/v1/history/devices"]
        self.assertEqual(len(history_calls), 2)
        self.assertEqual(self.db.get_cursor("t1"), NOW_MS - 1000)

        # 같은 이벤트가 다시 와도 중복 저장되지 않는다
        self.http.routes[("GET", "/v1/history/devices")] = [{"items": [ev(NOW_MS - 1000)], "_links": {}}]
        self.assertEqual(c.poll_history(), 0)

    def test_status_mode_polls_and_schedules(self):
        status = {"components": {"main": {"temperatureMeasurement": {
            "temperature": {"value": 21.0, "unit": "C", "timestamp": "2026-11-08T05:00:00Z"}}}}}
        self.http.add("GET", "/v1/devices/t1/status", status)
        c = self._collector(mode="status")
        c.refresh_devices()
        self.assertEqual(c.poll_status_due(), 1)
        self.assertEqual(c.poll_status_due(), 0)  # 아직 주기가 안 됨 → 요청하지 않음
        status_calls = [x for x in self.http.calls if x["path"] == "/v1/devices/t1/status"]
        self.assertEqual(len(status_calls), 1)
        # battery(6h)보다 온습도(5분) 주기가 짧으므로 300초
        self.assertEqual(c.status_interval(c.devices[0]), 300)


class RunLoopTest(CollectorTest):
    def test_run_once_in_history_mode_records_health(self):
        import threading

        self.http.add("GET", "/v1/history/devices", {"items": [ev(NOW_MS - 1000)], "_links": {}})
        c = self._collector()
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
        self.assertEqual(self.db.get_meta("last_ok_ms"), str(NOW_MS))


class IntervalReportTest(TempDirMixin, unittest.TestCase):
    def test_median_and_max(self):
        db = Database(self.tmp / "s.db")
        db.upsert_devices([{"device_id": "t1", "label": "거실", "name": None, "room_id": None,
                            "room_name": None, "location_id": "l", "capabilities": {"temperatureMeasurement"}}])
        base = NOW_MS - 3600_000
        rows = [("t1", "main", "temperatureMeasurement", "temperature", base + s * 1000, 20.0, None, "C", "history", None)
                for s in (0, 60, 120, 180, 600)]
        db.insert_readings(rows)
        rep = interval_report(db, 7, NOW_MS)
        self.assertEqual(len(rep), 1)
        self.assertEqual(rep[0]["count"], 5)
        self.assertEqual(rep[0]["median_s"], 60)
        self.assertEqual(rep[0]["max_s"], 420)
        db.close()


if __name__ == "__main__":
    unittest.main()
