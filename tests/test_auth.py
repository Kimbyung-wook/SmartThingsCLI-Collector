import base64
import unittest
import urllib.parse

from stcollector.auth import Authenticator, ReauthRequired, TokenStore
from stcollector.transport import HttpError

from .helpers import FakeHttp, TempDirMixin, make_config


class AuthTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg = make_config(self.tmp)
        self.http = FakeHttp()
        self.now = 1_000_000.0
        self.store = TokenStore(self.cfg.token_path)
        self.auth = Authenticator(self.cfg, self.store, http=self.http, clock=lambda: self.now)

    def test_authorize_url_has_readonly_scopes_and_state(self):
        url = self.auth.authorize_url("xyz")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(q["scope"], ["r:devices:* r:locations:*"])
        self.assertEqual(q["state"], ["xyz"])
        self.assertEqual(q["redirect_uri"], ["https://localhost/callback"])

    def test_parse_redirect(self):
        url = "https://localhost/callback?code=abc&state=s1"
        self.assertEqual(Authenticator.parse_redirect(url, "s1"), "abc")
        self.assertEqual(Authenticator.parse_redirect("  rawcode \n", "s1"), "rawcode")
        with self.assertRaises(ValueError):
            Authenticator.parse_redirect("https://localhost/callback?code=abc&state=evil", "s1")
        with self.assertRaises(ValueError):
            Authenticator.parse_redirect("https://localhost/callback?error=access_denied&state=s1", "s1")

    def test_exchange_saves_token_with_basic_auth(self):
        self.http.add("POST", "/oauth/token",
                      {"access_token": "A1", "refresh_token": "R1", "expires_in": 86399, "scope": "r:devices:*"})
        self.auth.exchange_code("abc")
        call = self.http.calls[0]
        expected = base64.b64encode(b"cid:secret").decode()
        self.assertEqual(call["headers"]["Authorization"], f"Basic {expected}")
        self.assertEqual(call["form"]["grant_type"], "authorization_code")
        saved = self.store.load()
        self.assertEqual(saved["access_token"], "A1")
        self.assertEqual(saved["expires_at"], self.now + 86399)

    def test_access_token_refreshes_near_expiry_and_rotates_refresh_token(self):
        self.store.save({"access_token": "A1", "refresh_token": "R1", "expires_at": self.now + 1800})
        self.http.add("POST", "/oauth/token", {"access_token": "A2", "refresh_token": "R2", "expires_in": 86399})
        self.assertEqual(self.auth.access_token(), "A2")
        self.assertEqual(self.http.calls[0]["form"]["refresh_token"], "R1")
        self.assertEqual(self.store.load()["refresh_token"], "R2")

    def test_valid_token_is_not_refreshed(self):
        self.store.save({"access_token": "A1", "refresh_token": "R1", "expires_at": self.now + 20000})
        self.assertEqual(self.auth.access_token(), "A1")
        self.assertEqual(self.http.calls, [])

    def test_rejected_refresh_requires_reauth(self):
        self.store.save({"access_token": "A1", "refresh_token": "R1", "expires_at": self.now - 1})
        self.http.add("POST", "/oauth/token", HttpError(400, '{"error":"invalid_grant"}'))
        with self.assertRaises(ReauthRequired):
            self.auth.access_token()

    def test_missing_token_requires_reauth(self):
        with self.assertRaises(ReauthRequired):
            self.auth.access_token()


if __name__ == "__main__":
    unittest.main()
