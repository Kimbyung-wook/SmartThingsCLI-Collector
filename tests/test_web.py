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
from stcollector.settings import RollupSettings, WebSettings
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
