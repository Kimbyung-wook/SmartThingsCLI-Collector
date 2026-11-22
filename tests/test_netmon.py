"""공유기 인터넷 송수신량: 읽기 액션 제한, UPnP 해석, 증분(랩어라운드·재부팅), 저장·집계, API, 설정."""

import json
import threading
import unittest
from pathlib import Path

from stcollector import netmon
from stcollector.config import ConfigError
from stcollector.netmon import (ALLOWED_ACTIONS, COUNTER_MOD, NetMonitor, NetStore, Reading, UpnpError, WanReader,
                                counter_delta, hour_rows, parse_control_url, parse_soap_response, parse_ssdp_location,
                                run_netmon, soap_request)
from stcollector.settings import NetmonSettings, load_netmon_settings, netmon_db_path
from stcollector.web import api
from tests.helpers import TempDirMixin

ROOT = Path(__file__).resolve().parent.parent
MIN = 60_000
H = 3_600_000
NOW = 1_767_538_800_000 + 30 * MIN  # 정시 30분 뒤 (KST 경계와 같은 시 경계)
ROUTER = "192.168.0.1"
LOCATION = f"http://{ROUTER}:5000/rootDesc.xml"

ROOT_DESC = f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"><device>
<deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
<deviceList><device><deviceType>urn:schemas-upnp-org:device:WANDevice:1</deviceType><serviceList>
<service><serviceType>urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1</serviceType>
<serviceId>urn:upnp-org:serviceId:WANCommonIFC1</serviceId><controlURL>/ctl/CmnIfCfg</controlURL>
<eventSubURL>/evt/CmnIfCfg</eventSubURL><SCPDURL>/WANCfg.xml</SCPDURL></service>
</serviceList></device></deviceList></device></root>""".encode()


def response(action, **fields):
    inner = "".join(f"<{k}>{v}</{k}>" for k, v in fields.items())
    return (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            f'<u:{action}Response xmlns:u="{netmon.WAN_COMMON}">{inner}</u:{action}Response></s:Body></s:Envelope>').encode()


class FakeRouter:
    """공유기 흉내. 받은 요청(URL, SOAPAction)을 모두 기록한다."""

    def __init__(self, rx=1000, tx=500):
        self.rx, self.tx = rx, tx
        self.calls = []
        self.fail = False

    def __call__(self, url, data=None, headers=None):
        self.calls.append((url, (headers or {}).get("SOAPAction")))
        if self.fail:
            raise UpnpError("timed out")
        if data is None:
            return ROOT_DESC
        action = headers["SOAPAction"].strip('"').split("#")[1]
        if action == "GetTotalBytesReceived":
            return response(action, NewTotalBytesReceived=self.rx)
        if action == "GetTotalBytesSent":
            return response(action, NewTotalBytesSent=self.tx)
        return response(action, NewWANAccessType="Ethernet", NewLayer1UpstreamMaxBitRate=100000000,
                        NewLayer1DownstreamMaxBitRate=100000000, NewPhysicalLinkStatus="Up")


def reader(router, location=LOCATION, discover=lambda ip: None, clock=lambda: NOW / 1000):
    return WanReader(ROUTER, location, http=router, discover=discover, clock=clock)


class ReadOnlyActionTests(unittest.TestCase):
    def test_allowed_actions_are_reads_only(self):
        self.assertEqual(ALLOWED_ACTIONS, {"GetTotalBytesReceived", "GetTotalBytesSent", "GetCommonLinkProperties"})
        self.assertTrue(all(a.startswith("Get") for a in ALLOWED_ACTIONS))

    def test_write_actions_are_refused_before_sending(self):
        router = FakeRouter()
        r = reader(router)
        for action in ("AddPortMapping", "DeletePortMapping", "SetEnabledForInternet", "ForceTermination",
                       "GetTotalBytesReceived ", "gettotalbytessent"):
            with self.subTest(action=action):
                with self.assertRaises(PermissionError):
                    soap_request(action)
                with self.assertRaises(PermissionError):
                    r.call(action)
        self.assertEqual(router.calls, [])  # 장치 설명 조회조차 하지 않았다

    def test_read_sends_only_allowed_actions_to_router(self):
        router = FakeRouter(rx=123, tx=45)
        got = reader(router).read()
        self.assertEqual((got.rx, got.tx), (123, 45))
        sent = [h.strip('"').split("#")[1] for _, h in router.calls if h]
        self.assertEqual(sent, ["GetTotalBytesReceived", "GetTotalBytesSent", "GetCommonLinkProperties"])
        self.assertTrue(set(sent) <= ALLOWED_ACTIONS)
        self.assertTrue(all(url.startswith(f"http://{ROUTER}:") for url, _ in router.calls))

    def test_module_has_no_write_action_names(self):
        source = (ROOT / "stcollector/netmon.py").read_text("utf-8")
        for name in ("AddPortMapping", "DeletePortMapping", "AddAnyPortMapping", "SetConnectionType", "RequestConnection"):
            self.assertNotIn(name, source)


class UpnpParseTests(unittest.TestCase):
    def test_control_url_from_root_desc(self):
        self.assertEqual(parse_control_url(ROOT_DESC, LOCATION), f"http://{ROUTER}:5000/ctl/CmnIfCfg")

    def test_dtd_is_refused(self):
        evil = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><r>&a;</r>'
        with self.assertRaises(UpnpError):
            parse_control_url(evil, LOCATION)
        with self.assertRaises(UpnpError):
            parse_soap_response(b"x" * (netmon.MAX_BODY + 1), "GetTotalBytesSent")

    def test_soap_fault_and_wrong_shape(self):
        fault = b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault/></s:Body></s:Envelope>'
        with self.assertRaises(UpnpError):
            parse_soap_response(fault, "GetTotalBytesSent")

    def test_ssdp_location(self):
        data = b"HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=120\r\nLocation: http://192.168.0.1:57078/rootDesc.xml\r\n\r\n"
        self.assertEqual(parse_ssdp_location(data), "http://192.168.0.1:57078/rootDesc.xml")
        self.assertIsNone(parse_ssdp_location(b"HTTP/1.1 200 OK\r\n\r\n"))

    def test_other_hosts_are_not_used(self):
        router = FakeRouter()
        r = reader(router, location="http://192.168.0.99:5000/rootDesc.xml", discover=lambda ip: "http://10.0.0.5/x.xml")
        with self.assertRaises(UpnpError):
            r.read()
        self.assertEqual(router.calls, [])

    def test_discovered_location_is_preferred_and_failure_rediscovers(self):
        router = FakeRouter()
        found = []
        r = reader(router, location=None, discover=lambda ip: found.append(ip) or f"http://{ip}:6000/rootDesc.xml")
        r.read()
        self.assertEqual(found, [ROUTER])
        self.assertTrue(r.control_url.startswith(f"http://{ROUTER}:6000/"))
        router.fail = True
        with self.assertRaises(UpnpError):
            r.read()
        self.assertIsNone(r.control_url)  # 다음 읽기에서 다시 찾는다


class DeltaTests(unittest.TestCase):
    LIMIT = 100e6

    def test_normal_and_wraparound(self):
        self.assertEqual(counter_delta(1000, 6000, MIN, self.LIMIT), 5000)
        self.assertEqual(counter_delta(COUNTER_MOD - 100, 400, MIN, self.LIMIT), 500)  # 4GB 에서 0으로

    def test_reboot_is_dropped(self):
        # 300MB 였다가 공유기 재부팅으로 2MB -> 2^32 을 더하면 1분에 약 4GB, 100Mbps 로 불가능
        self.assertIsNone(counter_delta(300_000_000, 2_000_000, MIN, self.LIMIT))

    def test_impossible_rate_and_bad_span(self):
        self.assertIsNone(counter_delta(0, 900_000_000, MIN, self.LIMIT))  # 1분에 900MB = 120Mbps
        self.assertIsNone(counter_delta(0, 10, 0, self.LIMIT))


class MonitorTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store = NetStore(self.tmp / "netmon" / "netmon.db")
        self.mon = NetMonitor(self.store, NetmonSettings(flush_s=10**6))

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def feed(self, points, link=None):
        for ts, rx, tx in points:
            self.mon.step(ts, None if rx is None else Reading(ts, rx, tx), link or {"down_bps": 100_000_000})

    def test_wrap_reboot_and_gaps(self):
        t = NOW - 2 * H
        self.feed([
            (t, COUNTER_MOD - 1_000_000, 10),
            (t + MIN, 4_000_000, 20),             # 랩어라운드: 5MB
            (t + 2 * MIN, 9_000_000, 30),         # 5MB
            (t + 3 * MIN, 1_000, 1),              # 공유기 재부팅 -> 버림
            (t + 4 * MIN, 2_001_000, 1001),       # 2MB
            (t + 5 * MIN, None, None),            # 한 번 실패
            (t + 6 * MIN, 4_001_000, 2001),       # 2분 구간, 남긴다
            (t + 7 * MIN, None, None), (t + 8 * MIN, None, None), (t + 9 * MIN, None, None),
            (t + 10 * MIN, 9_001_000, 3001),      # 4분 공백 (3주기 초과) -> 버림
            (t + 11 * MIN, 9_501_000, 3501),
        ])
        rows = [(r[1], r[2], r[3]) for r in self.mon.pending]
        self.assertEqual(rows, [(MIN, 5_000_000, 10), (MIN, 5_000_000, 10), (MIN, 2_000_000, 1000),
                                (2 * MIN, 2_000_000, 1000), (MIN, 500_000, 500)])
        self.assertEqual(self.mon.dropped, 2)

    def test_failure_is_reported_and_cleared(self):
        self.feed([(NOW, 1, 1), (NOW + MIN, None, None)])
        snap = self.mon.snapshot(NOW + MIN, {})
        self.assertEqual((snap["error_since"], snap["last"]), (NOW + MIN, None))
        self.feed([(NOW + 2 * MIN, 7_500_001, 1)])
        snap = self.mon.snapshot(NOW + 2 * MIN, {})
        self.assertIsNone(snap["error"])
        self.assertEqual(snap["last"]["rx_bps"], 500_000.0)  # 2분 동안 7.5MB = 0.5Mbps

    def test_flush_rolls_up_finished_hours(self):
        t = NOW - NOW % H - 2 * H  # 끝난 시 2개 + 진행 중인 시
        points, rx = [], 0
        for i in range(0, 150):
            rx += 750_000 if i != 30 else 7_500_000  # 1분 0.1Mbps, 한 번은 1Mbps
            points.append((t + i * MIN, rx, rx // 10))
        self.feed(points)
        self.mon.flush(t + 150 * MIN)
        hours = self.store.conn.execute("SELECT ts_ms, rx_bytes, covered_ms, rx_max_bps, samples FROM hours "
                                        "ORDER BY ts_ms").fetchall()
        # 첫 읽기는 기준값이라 첫 시는 59분, 끝 시각이 든 시에 넣는다
        self.assertEqual([h[0] for h in hours], [t, t + H])
        self.assertEqual(hours[0][1:], (59 * 750_000 - 750_000 + 7_500_000, 59 * MIN, 1e6, 59))
        self.assertEqual(hours[1][1:], (60 * 750_000, H, 1e5, 60))
        self.assertEqual(self.store.get_meta("hours_done"), str(t + 2 * H))
        self.assertEqual(self.mon.pending, [])

    def test_retention_keeps_hours(self):
        old = NOW - 30 * 86_400_000
        self.store.add_minutes([(old, MIN, 1, 1), (NOW, MIN, 2, 2)])
        self.store.rollup_hours(NOW)
        self.store.apply_retention(NOW, 14)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM minutes").fetchone()[0], 1)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM hours").fetchone()[0], 1)

    def test_hour_rows(self):
        self.assertEqual(hour_rows([(H + MIN, MIN, 600, 60), (H + 2 * MIN, 2 * MIN, 600, 0)]),
                         [(H, 1200, 60, 3 * MIN, 80.0, 8.0, 2)])


class RunTests(TempDirMixin, unittest.TestCase):
    def test_disabled_does_nothing(self):
        stop = threading.Event()
        self.assertEqual(run_netmon(self.tmp / "sensors.db", NetmonSettings(enabled=False), stop), 0)
        self.assertFalse(netmon_db_path(self.tmp / "sensors.db").exists())

    def test_failures_do_not_stop_the_loop(self):
        router = FakeRouter()
        router.fail = True
        stop = threading.Event()
        clock_ms = [NOW]

        def clock():
            clock_ms[0] += MIN
            if clock_ms[0] > NOW + MIN:
                stop.set()
            return clock_ms[0] / 1000

        settings = NetmonSettings(interval_s=30, runtime_file=str(self.tmp / "current.json"))
        r = WanReader(ROUTER, LOCATION, http=router, discover=lambda ip: None, clock=clock)
        with self.assertLogs("stcollector.netmon", "WARNING"):
            self.assertEqual(run_netmon(self.tmp / "sensors.db", settings, stop, reader=r, clock=clock), 0)
        data = json.loads((self.tmp / "current.json").read_text("utf-8"))
        self.assertIn("공유기", data["error"])


class SettingsTests(TempDirMixin, unittest.TestCase):
    def load(self, text):
        path = self.tmp / "c.toml"
        path.write_text(text, encoding="utf-8")
        return load_netmon_settings(path)

    def test_defaults_and_example(self):
        self.assertEqual(self.load(""), NetmonSettings())
        example = load_netmon_settings(ROOT / "config.example.toml")
        self.assertTrue(example.enabled)
        self.assertEqual((example.router, example.interval_s), ("192.168.0.1", 60))

    def test_invalid(self):
        for body in ('router = "8.8.8.8"', 'router = "공유기"', 'location = "http://10.0.0.1:1/x.xml"',
                     'location = "https://192.168.0.1/x.xml"', "interval_s = 5", 'enabled = "yes"'):
            with self.subTest(body=body):
                with self.assertRaises(ConfigError):
                    self.load("[netmon]\n" + body + "\n")

    def test_service_file_sandbox(self):
        text = (ROOT / "deploy/systemd/stcollector-netmon.service").read_text("utf-8")
        for line in ("IPAddressDeny=any", "IPAddressAllow=192.168.0.0/24 239.255.255.250/32",
                     "RestrictAddressFamilies=AF_INET AF_UNIX", "NoNewPrivileges=true", "ProtectSystem=strict",
                     "MemoryDenyWriteExecute=true", "ProtectProc=invisible", "StateDirectory=stcollector/netmon",
                     "InaccessiblePaths=-/var/lib/stcollector/cli -/var/lib/stcollector/token.json"):
            self.assertIn(line, text)
        self.assertNotIn("CAP_", text)
        # sysmon 은 그대로 네트워크 없이 돈다
        self.assertIn("PrivateNetwork=true", (ROOT / "deploy/systemd/stcollector-sysmon.service").read_text("utf-8"))


class NetworkApiTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.db_path = self.tmp / "sensors.db"
        self.current = self.tmp / "current.json"
        store = NetStore(netmon_db_path(self.db_path))
        hour = NOW - NOW % H
        # 3시간 전 시는 1시간 요약, 그 뒤는 1분 기록 (2시간 전 시 30분 + 진행 중인 시 20분), 마지막 5분은 저장 전
        store.add_minutes([(hour - 3 * H + i * MIN, MIN, 7_500_000, 750_000) for i in range(60)])
        store.rollup_hours(hour - 2 * H)
        store.add_minutes([(hour - 2 * H + i * MIN, MIN, 750_000, 75_000) for i in range(1, 31)])
        store.add_minutes([(hour + i * MIN, MIN, 750_000, 75_000) for i in range(1, 21)])
        store.close()
        pending = [[hour + i * MIN, MIN, 750_000, 75_000] for i in range(21, 26)]
        self.current.write_text(json.dumps({
            "ts_ms": NOW - 5_000, "interval_s": 60, "error": None, "error_since": None,
            "last": {"ts_ms": NOW - 5_000, "rx_bps": 100_000.0, "tx_bps": 10_000.0},
            "link": {"down_bps": 100_000_000, "up_bps": 100_000_000, "status": "Up"}, "pending": pending,
        }), "utf-8")
        self.hour = hour

    def ctx(self, enabled=True, now=NOW):
        return api.Context(None, now, self.db_path, netmon={"enabled": enabled, "db": netmon_db_path(self.db_path),
                                                            "current": self.current, "interval_s": 60})

    def test_hour_view(self):
        d = api.network(self.ctx(), {"period": "24h"})
        self.assertTrue(d["available"] and d["enabled"])
        self.assertEqual(d["buckets"][-1], self.hour)  # 진행 중인 시까지
        vol = {s["id"]: s for s in d["volume"]["series"]}
        spd = {s["id"]: s for s in d["speed"]["series"]}
        i = d["buckets"].index(self.hour - 3 * H)
        self.assertEqual((vol["rx"]["avg"][i], vol["tx"]["avg"][i], vol["rx"]["coverage"][i]), (0.45, 0.045, 1.0))
        self.assertEqual((spd["rx"]["avg"][i], spd["rx"]["max"][i]), (1.0, 1.0))
        self.assertIsNone(vol["rx"]["avg"][i + 2])  # 1시간 전은 기록 없음
        self.assertEqual(vol["rx"]["coverage"][i + 1], 0.5)  # 30분만
        last = len(d["buckets"]) - 1
        self.assertEqual(vol["rx"]["avg"][last], 0.01875)  # 저장 전 기록 5분까지 25분
        self.assertEqual(vol["rx"]["coverage"][last], round(25 / 30, 2))  # 지난 30분 중 25분
        self.assertEqual(d["oldest_ms"], self.hour - 3 * H)
        self.assertFalse(d["has_older"])
        c = d["current"]
        self.assertEqual((c["rx_mbps"], c["tx_mbps"], c["link_down_mbps"], c["stale"]), (0.1, 0.01, 100, False))

    def test_day_and_week_views_sum_hours(self):
        for period in ("month", "year"):
            d = api.network(self.ctx(), {"period": period})
            total = sum(v for v in d["volume"]["series"][0]["avg"] if v)
            self.assertAlmostEqual(total, (60 * 7.5e6 + 55 * 0.75e6) / 1e9, places=6)

    def test_older_chunk(self):
        d = api.network(self.ctx(), {"period": "24h", "end": str(self.hour - 2 * H)})
        self.assertEqual(d["buckets"][-1], self.hour - 3 * H)
        self.assertEqual(d["volume"]["series"][0]["avg"][-1], 0.45)

    def test_stale_current(self):
        c = api.network(self.ctx(now=NOW + 10 * MIN), {})["current"]
        self.assertTrue(c["stale"])
        self.assertIsNone(c["rx_mbps"])

    def test_disabled_and_missing(self):
        d = api.network(self.ctx(enabled=False), {})
        self.assertEqual((d["enabled"], d["available"], d["current"]), (False, False, None))
        empty = api.network(api.Context(None, NOW, self.tmp / "none" / "sensors.db"), {})
        self.assertFalse(empty["available"])
        self.assertIs(api.LIVE_ROUTES["/api/network"], api.network)


if __name__ == "__main__":
    unittest.main()
