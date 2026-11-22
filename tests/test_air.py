"""공기질: 수집 필터, 집계, API, 항목 묶기, 데이터 없음 처리."""

import tomllib
import unittest
from pathlib import Path

from stcollector.air import AIR_ATTRIBUTES, AIR_GRADES, AIR_METRICS, air_digits
from stcollector.collector import history_item_to_row
from stcollector.config import DEFAULT_ATTRIBUTES
from stcollector.db import Database
from stcollector.rollup import ROLLUP_ATTRIBUTES, run_rollup
from stcollector.settings import DEFAULT_MAX_HOLD_S, RollupSettings
from stcollector.web import api
from tests.helpers import TempDirMixin

ROOT = Path(__file__).resolve().parent.parent
H = 3_600_000
NOW = 1_767_538_800_000 + 30 * 60_000  # 정시 30분 뒤
T0 = NOW - NOW % H - 3 * H  # 끝난 시간 3개


def device(dev_id, room, caps):
    return {"device_id": dev_id, "label": f"{room} 기기", "name": "기기", "room_id": room, "room_name": room,
            "location_id": "loc", "capabilities": set(caps)}


def reading(dev, cap, attr, ts, num=None, text=None, unit=None):
    return (dev, "main", cap, attr, ts, num, text, unit, "history", None)


class CollectFilterTests(unittest.TestCase):
    def item(self, cap, attr, value=12, unit="μg/m^3"):
        return {"deviceId": "d1", "component": "main", "capability": cap, "attribute": attr, "value": value,
                "unit": unit, "epoch": NOW}

    def test_air_capabilities_are_collected(self):
        for cap, attrs in AIR_ATTRIBUTES.items():
            for attr in attrs:
                with self.subTest(cap=cap, attr=attr):
                    self.assertIsNotNone(history_item_to_row(self.item(cap, attr), DEFAULT_ATTRIBUTES))

    def test_unit_is_kept_and_other_attributes_are_ignored(self):
        row = history_item_to_row(self.item("tvocMeasurement", "tvocLevel", 0.12, "ppm"), DEFAULT_ATTRIBUTES)
        self.assertEqual((row[3], row[5], row[7]), ("tvocLevel", 0.12, "ppm"))
        # 같은 capability 의 다른 속성, 표준이 아닌 조합은 받지 않는다
        self.assertIsNone(history_item_to_row(self.item("fineDustSensor", "dustLevel"), DEFAULT_ATTRIBUTES))
        self.assertIsNone(history_item_to_row(self.item("airPurifierFanMode", "airPurifierFanMode", "auto"),
                                              DEFAULT_ATTRIBUTES))

    def test_grade_text_is_collected(self):
        row = history_item_to_row(self.item("fineDustHealthConcern", "fineDustHealthConcern", "moderate", None),
                                  DEFAULT_ATTRIBUTES)
        self.assertEqual((row[5], row[6]), (None, "moderate"))

    def test_example_config_matches_code_defaults(self):
        cfg = tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8"))
        for cap, attrs in AIR_ATTRIBUTES.items():
            self.assertEqual(cfg["collect"]["attributes"][cap], attrs)
        for attr in AIR_METRICS:
            self.assertEqual(cfg["rollup"]["max_hold_s"][attr], DEFAULT_MAX_HOLD_S[attr])
            self.assertIn(attr, ROLLUP_ATTRIBUTES)
        self.assertTrue(set(AIR_GRADES).isdisjoint(ROLLUP_ATTRIBUTES))  # 등급은 집계하지 않는다

    def test_digits(self):
        self.assertEqual(air_digits("tvocLevel", "ppm"), 3)
        self.assertEqual(air_digits("tvocLevel", "ppb"), 0)
        self.assertEqual(air_digits("formaldehydeLevel", "mg/m^3"), 3)
        self.assertEqual(air_digits("fineDustLevel", "μg/m^3"), 0)


class AirDbMixin(TempDirMixin):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "sensors.db"
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close()  # 임시 폴더를 지우기 전에 닫는다 (Windows)
        super().tearDown()

    def rollup(self):
        run_rollup(self.db, RollupSettings(retention_days=0), now_ms=NOW)

    def ctx(self):
        return api.Context(self.db.conn, NOW, self.path)

    def hourly(self, dev, cap, attr, values, unit):
        return [reading(dev, cap, attr, T0 + i * H, v, unit=unit) for i, v in enumerate(values)]


class AirRollupApiTests(AirDbMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.db.upsert_devices([
            device("air-1", "방A", ["dustSensor", "tvocMeasurement", "fineDustHealthConcern"]),
            device("air-2", "방B", ["fineDustSensor", "tvocMeasurement", "carbonDioxideMeasurement"]),
            device("clim-1", "방A", ["temperatureMeasurement"]),
        ])
        rows = []
        # 같은 PM2.5 가 기기마다 다른 capability 로 온다
        rows += self.hourly("air-1", "dustSensor", "fineDustLevel", [10, 20, 30], "μg/m^3")
        rows += self.hourly("air-2", "fineDustSensor", "fineDustLevel", [5, 6, 7], "μg/m^3")
        # TVOC 단위가 기기마다 다르다
        rows += self.hourly("air-1", "tvocMeasurement", "tvocLevel", [120, 130, 140], "ppb")
        rows += self.hourly("air-2", "tvocMeasurement", "tvocLevel", [0.1234, 0.2, 0.3], "ppm")
        rows += self.hourly("air-2", "carbonDioxideMeasurement", "carbonDioxide", [600, 900, 1200], "ppm")
        rows += self.hourly("clim-1", "temperatureMeasurement", "temperature", [21, 21, 21], "C")
        rows.append(reading("air-1", "fineDustHealthConcern", "fineDustHealthConcern", T0 + 2 * H, text="moderate"))
        self.db.insert_readings(rows)
        self.rollup()

    def test_pm25_from_two_capabilities_is_one_metric(self):
        d = api.series(self.ctx(), {"metric": "fineDustLevel", "period": "24h"})
        by_name = {x["name"]: x for x in d["series"]}
        self.assertEqual(set(by_name), {"방A", "방B"})
        i = d["buckets"].index(T0)
        self.assertEqual(by_name["방A"]["avg"][i:i + 3], [10.0, 20.0, 30.0])
        self.assertEqual(by_name["방B"]["avg"][i:i + 3], [5.0, 6.0, 7.0])
        self.assertEqual(d["unit"], "μg/m^3")
        self.assertEqual(by_name["방A"]["unit"], "μg/m^3")

    def test_mixed_units_are_kept_per_series(self):
        d = api.series(self.ctx(), {"metric": "tvocLevel", "period": "24h"})
        by_name = {x["name"]: x for x in d["series"]}
        i = d["buckets"].index(T0)
        self.assertIsNone(d["unit"])  # 한 축에 섞지 않도록 계열별 unit 을 본다
        self.assertEqual((by_name["방A"]["unit"], by_name["방A"]["digits"]), ("ppb", 0))
        self.assertEqual((by_name["방B"]["unit"], by_name["방B"]["digits"]), ("ppm", 3))
        self.assertEqual(by_name["방B"]["avg"][i], 0.123)

    def test_air_lists_only_present_metrics_with_units_and_grades(self):
        d = api.air(self.ctx(), {})
        self.assertEqual([m["key"] for m in d["metrics"]], ["fineDustLevel", "carbonDioxide", "tvocLevel"])
        tvoc = next(m for m in d["metrics"] if m["key"] == "tvocLevel")
        self.assertEqual(tvoc["units"], ["ppb", "ppm"])
        rooms = {r["name"]: r for r in d["rooms"]}
        pm = rooms["방A"]["values"]["fineDustLevel"]
        self.assertEqual((pm["now"], pm["unit"], pm["grade"]), (30.0, "μg/m^3", "moderate"))
        self.assertIsNone(rooms["방B"]["values"]["fineDustLevel"]["grade"])
        self.assertEqual(d["grades"]["moderate"], "보통")

    def test_summary_adds_air_only_where_present(self):
        rooms = {r["name"]: r for r in api.summary(self.ctx(), {})["rooms"]}
        self.assertEqual(set(rooms), {"방A", "방B"})  # 공기질만 있는 방도 카드가 생긴다
        self.assertEqual(rooms["방B"]["air"]["carbonDioxide"]["now"], 1200.0)
        self.assertNotIn("carbonDioxide", rooms["방A"]["air"])
        self.assertIn("temperature", rooms["방A"])

    def test_missing_metric_is_empty(self):
        d = api.series(self.ctx(), {"metric": "carbonMonoxideLevel", "period": "month"})
        self.assertEqual(d["series"], [])
        self.assertEqual(d["unit"], "ppm")
        self.assertFalse(d["has_older"])


class SameDeviceBothCapabilitiesTests(AirDbMixin, unittest.TestCase):
    def test_one_series_is_used_without_clobbering(self):
        self.db.upsert_devices([device("air-1", "방A", ["dustSensor", "fineDustSensor"])])
        self.db.insert_readings(self.hourly("air-1", "dustSensor", "fineDustLevel", [10, 10, 10], "μg/m^3")
                                + self.hourly("air-1", "fineDustSensor", "fineDustLevel", [90, 90, 90], "μg/m^3"))
        self.rollup()
        rows = self.db.conn.execute(
            "SELECT avg FROM rollup_hour WHERE device_id='air-1' AND attribute='fineDustLevel' ORDER BY bucket_start"
        ).fetchall()
        self.assertEqual([r[0] for r in rows], [10.0, 10.0, 10.0])  # capability 이름순 첫 번째 (dustSensor)
        d = api.series(self.ctx(), {"metric": "fineDustLevel", "period": "24h"})
        self.assertEqual(len(d["series"]), 1)


class NoAirTests(AirDbMixin, unittest.TestCase):
    def test_no_air_sensor(self):
        self.db.upsert_devices([device("clim-1", "방A", ["temperatureMeasurement"])])
        self.db.insert_readings(self.hourly("clim-1", "temperatureMeasurement", "temperature", [21, 22, 23], "C"))
        self.rollup()
        ctx = self.ctx()
        self.assertEqual(api.air(ctx, {}), {"metrics": [], "rooms": [], "grades": api.GRADE_TEXT,
                                            "closed_until": ctx.closed_until})
        self.assertNotIn("air", api.summary(ctx, {})["rooms"][0])
        self.assertIs(api.LIVE_ROUTES["/api/air"], api.air)

    def test_empty_database(self):
        ctx = self.ctx()
        self.assertEqual(api.air(ctx, {})["metrics"], [])
        self.assertEqual(api.series(ctx, {"metric": "fineDustLevel", "period": "24h"})["series"], [])


if __name__ == "__main__":
    unittest.main()
