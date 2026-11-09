import unittest

from stcollector.auth import Authenticator, TokenStore
from stcollector.client import SmartThingsClient
from stcollector.transport import HttpError

from .helpers import FakeHttp, TempDirMixin, make_config


class ClientTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg = make_config(self.tmp)
        self.http = FakeHttp()
        self.now = 1_000_000.0
        store = TokenStore(self.cfg.token_path)
        store.save({"access_token": "A1", "refresh_token": "R1", "expires_at": self.now + 50000})
        self.auth = Authenticator(self.cfg, store, http=self.http, clock=lambda: self.now)
        self.sleeps = []
        self.client = SmartThingsClient(self.cfg, self.auth, http=self.http, sleep=self.sleeps.append)

    def test_bearer_token_sent(self):
        self.http.add("GET", "/v1/devices/d1/status", {"components": {}})
        self.client.device_status("d1")
        self.assertEqual(self.http.calls[0]["headers"]["Authorization"], "Bearer A1")

    def test_refuses_to_send_token_to_foreign_host(self):
        with self.assertRaises(ValueError):
            self.client.get("https://evil.example.com/v1/devices")
        with self.assertRaises(ValueError):
            self.client.get("http://api.smartthings.com/v1/devices")

    def test_pagination_follows_next_link(self):
        self.http.add("GET", "/v1/devices",
                      {"items": [{"deviceId": "d1"}],
                       "_links": {"next": {"href": "https://api.smartthings.com/v1/devices?page=1"}}},
                      {"items": [{"deviceId": "d2"}], "_links": {}})
        ids = [d["deviceId"] for d in self.client.devices("loc-1")]
        self.assertEqual(ids, ["d1", "d2"])

    def test_401_refreshes_once_then_retries(self):
        self.http.add("GET", "/v1/locations", HttpError(401, "unauthorized"), {"items": []})
        self.http.add("POST", "/oauth/token", {"access_token": "A2", "refresh_token": "R2", "expires_in": 86399})
        self.client.locations()
        gets = [c for c in self.http.calls if c["method"] == "GET"]
        self.assertEqual(gets[-1]["headers"]["Authorization"], "Bearer A2")

    def test_429_waits_retry_after(self):
        self.http.add("GET", "/v1/locations", HttpError(429, "slow down", {"retry-after": "7"}), {"items": []})
        self.client.locations()
        self.assertEqual(self.sleeps, [7.0])

    def test_404_is_not_retried(self):
        self.http.add("GET", "/v1/history/devices", HttpError(404, "nope"))
        with self.assertRaises(HttpError):
            next(iter(self.client.device_history_pages("loc-1", "d1")))
        self.assertEqual(self.sleeps, [])

    def test_history_query_params(self):
        self.http.add("GET", "/v1/history/devices", {"items": []})
        list(self.client.device_history_pages("loc-1", "d1", limit=50))
        p = self.http.calls[0]["params"]
        self.assertEqual(p, {"locationId": "loc-1", "deviceId": "d1", "limit": 50, "oldestFirst": "false"})


if __name__ == "__main__":
    unittest.main()
