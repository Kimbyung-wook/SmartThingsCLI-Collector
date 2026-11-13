"""웹서버: 로그인, 접근 제어, API 입력 검증."""

import http.client
import ipaddress
import json
import threading
import unittest
import urllib.parse

from stcollector.config import ConfigError
from stcollector.db import Database
from stcollector.rollup import HOUR_MS, run_rollup
from stcollector.settings import RollupSettings, WebSettings, sysmon_db_path
from stcollector.sysmon import SysmonStore
from stcollector.web import api, server
from stcollector.web.session import LoginLimiter, Sessions, hash_password, valid_hash, verify_password
from tests.helpers import TempDirMixin

PASSWORD = "test-only-password"
HASH = hash_password(PASSWORD)
BASE = 1_767_538_800_000


def settings(**kw):
    values = dict(host="127.0.0.1", port=0, password_hash=HASH,
                  allowed_networks=(ipaddress.ip_network("192.168.0.0/24"),), session_days=30)
    values.update(kw)
    return WebSettings(**values)


class PasswordTests(unittest.TestCase):
    def test_hash_roundtrip(self):
        self.assertTrue(valid_hash(HASH))
        self.assertTrue(verify_password(PASSWORD, HASH))
        self.assertFalse(verify_password("wrong", HASH))
        self.assertNotIn(PASSWORD, HASH)

    def test_invalid_hash(self):
        self.assertFalse(valid_hash("plain-text"))
        self.assertFalse(valid_hash("scrypt$1$1$1$a$b"))

    def test_session_tamper_and_expiry(self):
        now = [1000.0]
        s = Sessions(HASH, 1, clock=lambda: now[0])
        token = s.create()
        self.assertTrue(s.valid(token))
        self.assertFalse(s.valid(token[:-2] + "xx"))
        self.assertFalse(s.valid("1.2.3"))
        self.assertFalse(s.valid(None))
        now[0] += 86401
        self.assertFalse(s.valid(token))

    def test_session_revocation_and_password_change(self):
        s = Sessions(HASH, 1)
        token = s.create()
        self.assertFalse(Sessions(hash_password("another-one"), 1).valid(token))
        s.revoke(token)
        self.assertFalse(s.valid(token))

    def test_limiter(self):
        now = [0.0]
        lim = LoginLimiter(clock=lambda: now[0])
        for _ in range(5):
            self.assertFalse(lim.blocked("a"))
            lim.fail("a")
        self.assertTrue(lim.blocked("a"))
        self.assertFalse(lim.blocked("b"))
        now[0] = 301
        self.assertFalse(lim.blocked("a"))


class ServerTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.db_path = self.tmp / "s.db"
        db = Database(self.db_path)
        db.upsert_devices([
            {"device_id": "dev-1", "label": "거실 온습도", "name": "x", "room_id": "r1", "room_name": "거실",
             "location_id": "l", "capabilities": {"temperatureMeasurement"}},
        ])
        db.insert_readings([("dev-1", "main", "temperatureMeasurement", "temperature", BASE, 21.5, None, "C", "history", None)])
        run_rollup(db, RollupSettings(retention_days=0), now_ms=BASE + 2 * HOUR_MS)
        db.close()
        self.app = server.App(self.db_path, settings())
        self.srv = server.Server(("127.0.0.1", 0), self.app)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(5)
        super().tearDown()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        hdrs = dict(r.getheaders())
        c.close()
        return r.status, hdrs, data

    def login(self):
        status, hdrs, _ = self.req("POST", "/login", urllib.parse.urlencode({"password": PASSWORD}))
        self.assertEqual(status, 303)
        return hdrs["Set-Cookie"].split(";")[0]

    def test_requires_login(self):
        self.assertEqual(self.req("GET", "/")[0], 303)
        self.assertEqual(self.req("GET", "/api/summary")[0], 401)
        self.assertEqual(self.req("GET", "/static/app.js")[0], 401)

    def test_public_assets(self):
        self.assertEqual(self.req("GET", "/login")[0], 200)
        status, hdrs, _ = self.req("GET", "/manifest.webmanifest")
        self.assertEqual(status, 200)
        self.assertIn("manifest", hdrs["Content-Type"])
        self.assertEqual(self.req("GET", "/static/icons/icon-192.png")[0], 200)

    def test_login_flow(self):
        _, hdrs, _ = self.req("POST", "/login", "password=wrong")
        self.assertEqual(hdrs["Location"], "/login?e=bad")
        cookie = self.login()
        self.assertIn("st_session=", cookie)
        status, _, data = self.req("GET", "/api/summary", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["has_data"])

    def test_login_message_is_fixed_text(self):
        _, _, body = self.req("GET", "/login?e=<script>")
        self.assertNotIn(b"<script>", body)
        _, _, body = self.req("GET", "/login?e=bad")
        self.assertIn("비밀번호가 맞지 않습니다".encode(), body)

    def test_cookie_flags(self):
        _, hdrs, _ = self.req("POST", "/login", urllib.parse.urlencode({"password": PASSWORD}))
        self.assertIn("HttpOnly", hdrs["Set-Cookie"])
        self.assertIn("SameSite=Lax", hdrs["Set-Cookie"])

    def test_logout_invalidates(self):
        cookie = self.login()
        self.req("POST", "/logout", "", {"Cookie": cookie})
        self.assertEqual(self.req("GET", "/api/summary", headers={"Cookie": cookie})[0], 401)

    def test_login_lockout(self):
        for _ in range(5):
            self.req("POST", "/login", "password=wrong")
        _, hdrs, _ = self.req("POST", "/login", urllib.parse.urlencode({"password": PASSWORD}))
        self.assertEqual(hdrs["Location"], "/login?e=locked")

    def test_cross_origin_post_rejected(self):
        status, _, _ = self.req("POST", "/login", "password=x", {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_referrer_policy_keeps_same_origin_header(self):
        # no-referrer 면 브라우저의 로그인 폼이 Origin: null 로 전송되어 위 검사에서 거부된다.
        _, hdrs, _ = self.req("GET", "/login")
        self.assertEqual(hdrs["Referrer-Policy"], "same-origin")

    def test_body_limit(self):
        self.assertEqual(self.req("POST", "/login", "a" * 5000)[0], 413)

    def test_security_headers(self):
        _, hdrs, _ = self.req("GET", "/login")
        csp = hdrs["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("http", csp)
        self.assertEqual(hdrs["X-Content-Type-Options"], "nosniff")

    def test_input_validation(self):
        cookie = {"Cookie": self.login()}
        for path in ("/api/series?period=forever", "/api/series?metric=../../etc",
                     "/api/lux-dist?device=%27%20OR%201=1--", "/api/lux-dist?device=unknown-id",
                     "/api/lux-dist?scope=x", "/api/plugs?period=1"):
            self.assertEqual(self.req("GET", path, headers=cookie)[0], 400, path)
        self.assertEqual(self.req("GET", "/api/nope", headers=cookie)[0], 404)
        self.assertEqual(self.req("GET", "/static/../server.py", headers=cookie)[0], 404)

    def test_all_routes_return_json(self):
        cookie = {"Cookie": self.login()}
        paths = ["/api/meta", "/api/summary", "/api/status", "/api/occupancy", "/api/occupancy?period=year",
                 "/api/plugs", "/api/plugs?period=year"]
        paths += [f"/api/series?metric={m}&period={p}" for m in api.TREND_METRICS for p in api.TREND_PERIODS]
        paths += [f"/api/lux-dist?period={p}" for p in api.DIST_PERIODS]
        for path in paths:
            status, _, data = self.req("GET", path, headers=cookie)
            self.assertEqual(status, 200, path)
            json.loads(data)

    def test_no_external_urls_in_pages(self):
        for name, file in self.app.static.items():
            if name.endswith((".html", ".js", ".css", ".webmanifest")):
                text = file.body.decode().replace("http://www.w3.org/2000/svg", "")
                self.assertNotRegex(text, r"https?://", name)


class AccessTests(TempDirMixin, unittest.TestCase):
    def test_refuses_without_password(self):
        with self.assertRaises(ConfigError):
            server.App(self.tmp / "x.db", settings(password_hash=""))

    def test_refuses_plain_password(self):
        with self.assertRaises(ConfigError):
            server.App(self.tmp / "x.db", settings(password_hash="hunter2"))

    def test_allowed_networks(self):
        app = server.App(self.tmp / "x.db", settings())
        self.assertTrue(app.allowed_ip("192.168.0.50"))
        self.assertTrue(app.allowed_ip("127.0.0.1"))
        self.assertTrue(app.allowed_ip("::ffff:192.168.0.9"))
        self.assertFalse(app.allowed_ip("192.168.1.50"))
        self.assertFalse(app.allowed_ip("8.8.8.8"))


class HubStatusTests(TempDirMixin, unittest.TestCase):
    """SmartThings 허브용 /hub/status: 허용 주소만, 로그인 없이, 이름·비밀값 없이."""

    def start(self, allow, now_ms=BASE + 3 * HOUR_MS, current=None):
        db_path = self.tmp / "h.db"
        db = Database(db_path)
        db.upsert_devices([
            {"device_id": "dev-1", "label": "시험 센서", "name": "x", "room_id": "r1", "room_name": "시험 방",
             "location_id": "loc-test", "capabilities": {"temperatureMeasurement"}},
        ])
        db.set_meta("last_ok_ms", now_ms - 5 * 60_000)
        db.set_meta("auth_state", "ok")
        db.set_meta("last_error", "secret-looking error text")
        db.close()
        runtime = self.tmp / "current.json"
        if current is not None:
            runtime.write_text(json.dumps(current), "utf-8")
        app = server.App(db_path, settings(hub_status_allow=tuple(ipaddress.ip_address(a) for a in allow)),
                         clock=lambda: now_ms / 1000, sysmon_current=runtime)
        srv = server.Server(("127.0.0.1", 0), app)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()

        def stop():
            srv.shutdown()
            srv.server_close()
            thread.join(5)
        self.addCleanup(stop)
        return srv.server_address[1]

    def get(self, port, path="/hub/status", method="GET"):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request(method, path)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, dict(r.getheaders()), data

    def test_closed_by_default(self):
        port = self.start([])
        self.assertEqual(self.get(port)[0], 404)

    def test_other_address_gets_404(self):
        port = self.start(["192.168.0.20"])
        self.assertEqual(self.get(port)[0], 404)

    def test_allowed_address_without_login(self):
        now = BASE + 3 * HOUR_MS
        port = self.start(["127.0.0.1"], now_ms=now, current={
            "ts_ms": now - 3000, "temp_c": 51.234, "freq_mhz": 900.0, "cpu_pct": 80.0, "cpu_pct_1m": 12.34,
        })
        status, hdrs, data = self.get(port)
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["Cache-Control"], "no-store")
        body = json.loads(data)
        self.assertTrue(body["collect_ok"])
        self.assertEqual(body["collect_state"], "ok")
        self.assertEqual(body["collect_text"], "정상")
        self.assertEqual(body["cpu_temp_c"], 51.2)
        self.assertEqual(body["cpu_mhz"], 900)
        self.assertEqual(body["cpu_pct"], 12.3)  # 순간값이 아니라 1분 평균
        self.assertIsInstance(body["disk_used_pct"], float)
        self.assertRegex(body["last_ok_text"], r"^\d\d-\d\d \d\d:\d\d$")
        text = data.decode()
        for secret in ("시험 센서", "시험 방", "loc-test", "dev-1", "secret-looking"):
            self.assertNotIn(secret, text)
        self.assertEqual(self.get(port, method="HEAD")[0], 200)

    def test_stale_sysmon_values_are_null(self):
        now = BASE + 3 * HOUR_MS
        port = self.start(["127.0.0.1"], now_ms=now, current={"ts_ms": now - 120_000, "temp_c": 50.0})
        body = json.loads(self.get(port)[2])
        self.assertIsNone(body["cpu_temp_c"])
        self.assertFalse(body["sysmon_fresh"])

    def test_cpu_pct_falls_back_to_instant_value(self):
        now = BASE + 3 * HOUR_MS
        port = self.start(["127.0.0.1"], now_ms=now, current={"ts_ms": now - 1000, "cpu_pct": 33.3})
        self.assertEqual(json.loads(self.get(port)[2])["cpu_pct"], 33.3)

    def minutes(self, rows):
        store = SysmonStore(sysmon_db_path(self.tmp / "h.db"))
        base = dict(temp_min=0, temp_max=0, freq_avg=900.0, load_avg=0.1, mem_avail_mb=1, disk_free_gb=1,
                    undervolt_s=0, overheat_s=0, throttle_s=0, samples=12)
        store.add_minutes([dict(base, ts_ms=ts, temp_avg=t, cpu_avg=c) for ts, t, c in rows])
        store.close()

    def test_temperature_is_last_minute_and_cpu_is_five_minute_average(self):
        now = BASE + 3 * HOUR_MS + 20_000  # 분 시작 20초 뒤
        m = now - now % 60_000
        self.minutes([(m - 360_000, 70.0, 90.0),   # 6분 전: 5분 창 밖
                      (m - 300_000, 50.0, 10.0), (m - 240_000, 51.0, 20.0), (m - 180_000, 52.0, None),
                      (m - 120_000, 53.0, 30.0)])
        port = self.start(["127.0.0.1"], now_ms=now, current={
            "ts_ms": now - 2000, "temp_c": 60.0, "cpu_pct": 99.0, "cpu_pct_1m": 88.0,
            "pending": [{"ts_ms": m - 60_000, "temp_avg": 54.44, "cpu_avg": 40.0},   # 아직 DB에 없는 끝난 분
                        {"ts_ms": m, "temp_avg": 65.0, "cpu_avg": 100.0}],          # 진행 중인 분: 뺌
        })
        body = json.loads(self.get(port)[2])
        self.assertEqual(body["cpu_temp_c"], 54.4)
        self.assertEqual(body["cpu_pct"], 25.0)  # (10 + 20 + 30 + 40) / 4, 빈 값은 뺌

    def test_old_minute_temperature_falls_back_to_current(self):
        now = BASE + 3 * HOUR_MS
        m = now - now % 60_000
        self.minutes([(m - 300_000, 50.0, 10.0)])  # 5분 전 분은 온도로 쓰기엔 오래됨
        port = self.start(["127.0.0.1"], now_ms=now, current={"ts_ms": now - 1000, "temp_c": 61.2, "cpu_pct": 1.0})
        body = json.loads(self.get(port)[2])
        self.assertEqual(body["cpu_temp_c"], 61.2)
        self.assertEqual(body["cpu_pct"], 10.0)

    def test_stale_monitor_ignores_minutes(self):
        now = BASE + 3 * HOUR_MS
        m = now - now % 60_000
        self.minutes([(m - 60_000, 50.0, 10.0)])
        port = self.start(["127.0.0.1"], now_ms=now, current={"ts_ms": now - 120_000, "temp_c": 50.0})
        body = json.loads(self.get(port)[2])
        self.assertIsNone(body["cpu_temp_c"])
        self.assertIsNone(body["cpu_pct"])

    def test_other_paths_still_need_login(self):
        port = self.start(["127.0.0.1"])
        self.assertEqual(self.get(port, "/api/status")[0], 401)
        self.assertEqual(self.get(port, "/hub/status/x")[0], 404)


class FakeMetaCtx:
    def __init__(self, now_ms, **meta):
        self.now_ms, self._meta = now_ms, meta

    def meta(self, key):
        value = self._meta.get(key)
        return None if value is None else str(value)

    meta_int = api.Context.meta_int


class CollectHealthTests(unittest.TestCase):
    NOW = BASE

    def health(self, **meta):
        return api.collect_health(FakeMetaCtx(self.NOW, **meta))

    def test_ok(self):
        self.assertEqual(self.health(last_ok_ms=self.NOW - 60_000), ("ok", True, None))

    def test_never_collected(self):
        self.assertEqual(self.health()[:2], ("unknown", False))

    def test_reauth(self):
        self.assertEqual(self.health(last_ok_ms=self.NOW, auth_state="reauth_required")[:2], ("reauth", False))

    def test_stopped_after_30_minutes(self):
        self.assertEqual(self.health(last_ok_ms=self.NOW - 30 * 60_000)[:2], ("ok", True))
        self.assertEqual(self.health(last_ok_ms=self.NOW - 31 * 60_000), ("stopped", False, 31))

    def test_history_failure_alerts_after_an_hour(self):
        recent = self.NOW - 60_000
        self.assertEqual(self.health(last_ok_ms=recent, history_fail_since_ms=self.NOW - 5 * 60_000)[:2], ("ok", True))
        self.assertEqual(self.health(last_ok_ms=recent, history_fail_since_ms=self.NOW - 20 * 60_000),
                         ("history", True, 20))
        self.assertEqual(self.health(last_ok_ms=recent, history_fail_since_ms=self.NOW - 61 * 60_000),
                         ("history", False, 61))


class HubSettingsTests(TempDirMixin, unittest.TestCase):
    def load(self, text):
        from stcollector.settings import load_web_settings
        path = self.tmp / "c.toml"
        path.write_text(text, "utf-8")
        return load_web_settings(path)

    def test_default_empty(self):
        self.assertEqual(self.load("[web]\n").hub_status_allow, ())

    def test_addresses(self):
        s = self.load('[web]\nhub_status_allow = ["192.168.0.20"]\n')
        self.assertEqual(s.hub_status_allow, (ipaddress.ip_address("192.168.0.20"),))

    def test_rejects_network(self):
        with self.assertRaises(ConfigError):
            self.load('[web]\nhub_status_allow = ["192.168.0.0/24"]\n')
        with self.assertRaises(ConfigError):
            self.load('[web]\nhub_status_allow = "192.168.0.20"\n')


class ApiMathTests(unittest.TestCase):
    def test_dew_point(self):
        self.assertAlmostEqual(api.dew_point(25.0, 50.0), 13.86, places=1)
        self.assertAlmostEqual(api.dew_point(20.0, 100.0), 20.0, places=1)

    def test_most_time_range_narrowest(self):
        r = api.most_time_range([1, 1, 40, 40, 1, 1, 16])
        self.assertEqual((r["from"], r["to"]), (2, 3))
        self.assertIsNone(api.most_time_range([0, 0, 0]))

    def test_most_time_range_reaches_share(self):
        r = api.most_time_range([45, 40, 15, 40, 45])
        self.assertGreaterEqual(r["share"], 0.8)


if __name__ == "__main__":
    unittest.main()
