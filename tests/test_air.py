"""공기질: 수집 필터, 집계, API, 항목 묶기, 중복 이벤트, 온습도 분리, 데이터 없음 처리."""

import tomllib
import unittest
from pathlib import Path

from stcollector.air import AIR_ATTRIBUTES, AIR_GRADES, AIR_METRICS, air_digits, display_unit, keep_reading
from stcollector.collector import Collector, history_item_to_row
from stcollector.config import DEFAULT_ATTRIBUTES
from stcollector.db import Database
from stcollector.rollup import ROLLUP_ATTRIBUTES, run_rollup
from stcollector.config import ConfigError
from stcollector.settings import DEFAULT_MAX_HOLD_S, RollupSettings, load_web_settings
from stcollector.web import api
from tests.helpers import TempDirMixin

ROOT = Path(__file__).resolve().parent.parent
H = 3_600_000
NOW = 1_767_538_800_000 + 30 * 60_000  # 정시 30분 뒤
T0 = NOW - NOW % H - 3 * H  # 끝난 시간 3개


def device(dev_id, room, caps):
    return {"device_id": dev_id, "label": f"{room} 기기", "name": "기기", "room_id": room, "room_name": room,
            "location_id": "loc", "capabilities": set(caps)}


def reading(dev, cap, attr, ts, num=None, text=None, unit=None, component="main"):
    return (dev, component, cap, attr, ts, num, text, unit, "history", None)


# PV28-CW 프로필 (드라이버가 보내는 capability)
PV28 = ["carbonDioxideMeasurement", "fineDustSensor", "dustSensor", "veryFineDustSensor", "formaldehydeMeasurement",
        "tvocMeasurement", "temperatureMeasurement", "relativeHumidityMeasurement", "battery", "powerSource", "switch"]


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
        self.assertIs(cfg["dashboard"]["air_quality_temp_in_rooms"], False)

    def test_digits(self):
        self.assertEqual(air_digits("tvocLevel", "ppm"), 3)
        self.assertEqual(air_digits("tvocLevel", "ppb"), 0)
        self.assertEqual(air_digits("formaldehydeLevel", "mg/m^3"), 3)
        self.assertEqual(air_digits("fineDustLevel", "μg/m^3"), 0)
        self.assertEqual(air_digits("tvocLevel", "ug/m3"), 0)  # PV28-CW: 정수 μg/m³

    def test_display_unit(self):
        for raw in ("μg/m^3", "\u00b5g/m^3", "ug/m3"):
            self.assertEqual(display_unit(raw), "μg/m³")
        self.assertEqual(display_unit("mg/m^3"), "mg/m³")
        self.assertEqual(display_unit("ppm"), "ppm")
        self.assertIsNone(display_unit(None))
        self.assertIsNone(display_unit(""))


class KeepReadingTests(unittest.TestCase):
    def test_pm25_duplicate_is_dropped_only_when_fine_dust_sensor_exists(self):
        self.assertFalse(keep_reading(set(PV28), "main", "dustSensor", "fineDustLevel"))
        self.assertTrue(keep_reading(set(PV28), "main", "fineDustSensor", "fineDustLevel"))
        self.assertTrue(keep_reading(set(PV28), "main", "dustSensor", "dustLevel"))  # PM10 은 그대로
        self.assertTrue(keep_reading({"dustSensor"}, "main", "dustSensor", "fineDustLevel"))  # 다른 기기의 PM2.5
        self.assertTrue(keep_reading(None, "main", "dustSensor", "fineDustLevel"))  # 모르는 기기는 받는다

    def test_air_device_setting_switch_is_dropped(self):
        self.assertFalse(keep_reading(set(PV28), "alarmSound", "switch", "switch"))  # 경보음 설정
        self.assertTrue(keep_reading(set(PV28), "main", "switch", "switch"))
        self.assertTrue(keep_reading({"switch", "powerMeter"}, "outlet2", "switch", "switch"))  # 멀티탭

    def test_collector_filters_with_device_capabilities(self):
        c = Collector.__new__(Collector)  # 필터만 본다 (네트워크·DB 없이)
        c._caps, c._caps_of = {}, None
        c.devices = [device("air-1", "방A", PV28), device("dust-1", "방B", ["dustSensor"])]
        self.assertFalse(c._keep(reading("air-1", "dustSensor", "fineDustLevel", NOW, 9)))
        self.assertTrue(c._keep(reading("dust-1", "dustSensor", "fineDustLevel", NOW, 9)))
        self.assertFalse(c._keep(reading("air-1", "switch", "switch", NOW, text="on", component="alarmSound")))
        c.devices = [device("air-1", "방A", ["dustSensor"])]  # 기기 목록이 바뀌면 다시 본다
        self.assertTrue(c._keep(reading("air-1", "dustSensor", "fineDustLevel", NOW, 9)))


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

    def ctx(self, **kw):
        return api.Context(self.db.conn, NOW, self.path, **kw)

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
        self.assertEqual(d["unit"], "μg/m³")  # 표시 단위로 다듬는다 (값은 그대로)
        self.assertEqual(by_name["방A"]["unit"], "μg/m³")

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
        self.assertEqual((pm["now"], pm["unit"], pm["grade"]), (30.0, "μg/m³", "moderate"))
        self.assertEqual(tvoc["note"], "추정값(추세용)")
        self.assertIsNone(d["metrics"][0]["note"])  # PM2.5
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
        self.assertEqual([r[0] for r in rows], [90.0, 90.0, 90.0])  # fineDustSensor 를 먼저 고른다
        d = api.series(self.ctx(), {"metric": "fineDustLevel", "period": "24h"})
        self.assertEqual(len(d["series"]), 1)
        i = d["buckets"].index(T0)
        self.assertEqual(d["series"][0]["avg"][i], 90.0)
        self.assertEqual(api.air(self.ctx(), {})["rooms"][0]["values"]["fineDustLevel"]["now"], 90.0)


class Pv28UnitsTests(AirDbMixin, unittest.TestCase):
    def test_pv28_units_are_tidied_for_display(self):
        self.db.upsert_devices([device("air-1", "방A", PV28)])
        self.db.insert_readings(self.hourly("air-1", "tvocMeasurement", "tvocLevel", [320, 330, 340], "ug/m3")
                                + self.hourly("air-1", "formaldehydeMeasurement", "formaldehydeLevel",
                                              [0.012, 0.015, 0.0184], "mg/m^3"))
        self.rollup()
        tvoc = api.series(self.ctx(), {"metric": "tvocLevel", "period": "24h"})
        hcho = api.series(self.ctx(), {"metric": "formaldehydeLevel", "period": "24h"})
        self.assertEqual((tvoc["unit"], tvoc["series"][0]["digits"]), ("μg/m³", 0))
        self.assertEqual((hcho["unit"], hcho["series"][0]["digits"]), ("mg/m³", 3))
        i = hcho["buckets"].index(T0)
        self.assertEqual(hcho["series"][0]["avg"][i + 2], 0.018)
        stored = self.db.conn.execute("SELECT DISTINCT unit FROM readings WHERE attribute='tvocLevel'").fetchall()
        self.assertEqual([tuple(r) for r in stored], [("ug/m3",)])  # 저장은 기기 단위 그대로


class DuplicateEventTests(AirDbMixin, unittest.TestCase):
    """드라이버 시작, 허브 재부팅, 앱 새로고침 때 같은 값이 다시 온다."""

    def hour_row(self):
        return self.db.conn.execute(
            "SELECT avg, min, max, coverage FROM rollup_hour WHERE device_id='air-1' AND attribute='carbonDioxide' "
            "AND bucket_start=?", (T0,)).fetchone()

    def test_reemitted_same_value_does_not_change_rollup(self):
        self.db.upsert_devices([device("air-1", "방A", PV28)])
        m = 60_000
        rows = [reading("air-1", "carbonDioxideMeasurement", "carbonDioxide", T0 - 10 * m, 600, unit="ppm"),
                reading("air-1", "carbonDioxideMeasurement", "carbonDioxide", T0 + 20 * m, 600, unit="ppm"),  # 재시작
                reading("air-1", "carbonDioxideMeasurement", "carbonDioxide", T0 + 30 * m, 600, unit="ppm"),  # 새로고침
                reading("air-1", "carbonDioxideMeasurement", "carbonDioxide", T0 + 45 * m, 900, unit="ppm"),
                reading("air-1", "carbonDioxideMeasurement", "carbonDioxide", T0 + 45 * m, 900, unit="ppm")]  # 똑같은 이벤트
        self.assertEqual(self.db.insert_readings(rows), 4)  # 완전히 같은 행은 한 번만 저장
        self.db.insert_readings(rows[1:2])  # 다음 수집에서 다시 받아도 그대로
        self.rollup()
        avg, lo, hi, cov = self.hour_row()
        self.assertAlmostEqual(avg, (45 * 600 + 15 * 900) / 60)  # 재전송이 없을 때와 같다
        self.assertEqual((lo, hi, cov), (600, 900, 1.0))

    def test_gap_longer_than_max_hold_is_missing(self):
        self.db.upsert_devices([device("air-1", "방A", PV28)])
        # 끝난 3시간 앞에서 보고가 멈춘다 -> 2시간까지만 유지하고 그 뒤는 비어 있다
        self.db.insert_readings([reading("air-1", "carbonDioxideMeasurement", "carbonDioxide", T0 - H, 700, unit="ppm")])
        self.rollup()
        rows = dict(self.db.conn.execute(
            "SELECT bucket_start, coverage FROM rollup_hour WHERE device_id='air-1' AND attribute='carbonDioxide'"))
        self.assertEqual(rows.get(T0), 1.0)
        self.assertIsNone(rows.get(T0 + H))  # 1시간 + 2시간 뒤는 데이터 없음
        self.assertEqual(DEFAULT_MAX_HOLD_S["carbonDioxide"], 7_200)


class AirClimateTests(AirDbMixin, unittest.TestCase):
    """공기질 기기의 온습도는 기본으로 방별 온습도와 섞지 않는다."""

    def setUp(self):
        super().setUp()
        self.db.upsert_devices([device("clim-1", "방A", ["temperatureMeasurement", "relativeHumidityMeasurement"]),
                                device("air-1", "방A", PV28)])
        self.db.insert_readings(
            self.hourly("clim-1", "temperatureMeasurement", "temperature", [21.5, 21.5, 21.5], "C")
            + self.hourly("air-1", "temperatureMeasurement", "temperature", [24, 24, 24], "C")
            + self.hourly("air-1", "relativeHumidityMeasurement", "humidity", [50, 51, 52], "%")
            + self.hourly("air-1", "carbonDioxideMeasurement", "carbonDioxide", [600, 700, 800], "ppm"))
        self.rollup()

    def test_default_keeps_air_device_out_of_rooms(self):
        ctx = self.ctx()
        ids = [s["id"] for s in api.series(ctx, {"metric": "temperature", "period": "24h"})["series"]]
        self.assertEqual(ids, ["clim-1"])
        room = api.summary(ctx, {})["rooms"][0]
        self.assertEqual(room["temperature"]["now"], 21.5)
        self.assertNotIn("humidity", room)  # 습도는 공기질 기기에만 있다 -> 방 카드에 넣지 않는다
        self.assertEqual(api.series(ctx, {"metric": "humidity", "period": "24h"})["series"], [])

    def test_air_tab_shows_only_air_metrics(self):
        # 온습도는 다른 탭에서 보므로 공기질 탭에는 공기질 항목만 둔다
        for flag in (False, True):
            d = api.air(self.ctx(air_temp_in_rooms=flag), {})
            self.assertEqual([m["key"] for m in d["metrics"]], ["carbonDioxide"])
            self.assertEqual(set(d["rooms"][0]["values"]), {"carbonDioxide"})

    def test_switch_mixes_air_device_into_rooms(self):
        ctx = self.ctx(air_temp_in_rooms=True)
        ids = [s["id"] for s in api.series(ctx, {"metric": "temperature", "period": "24h"})["series"]]
        self.assertEqual(sorted(ids), ["air-1", "clim-1"])

    def test_setting_is_loaded(self):
        path = self.tmp / "c.toml"
        path.write_text("[web]\n", encoding="utf-8")
        self.assertFalse(load_web_settings(path).air_quality_temp_in_rooms)
        path.write_text("[web]\n[dashboard]\nair_quality_temp_in_rooms = true\n", encoding="utf-8")
        self.assertTrue(load_web_settings(path).air_quality_temp_in_rooms)
        path.write_text("[web]\n[dashboard]\nair_quality_temp_in_rooms = \"yes\"\n", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_web_settings(path)


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
