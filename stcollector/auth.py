"""SmartThings OAuth 2.0 (Authorization Code) 인증과 토큰 자동 갱신.

- 액세스 토큰: 약 24시간 유효. 만료 1시간 전부터 갱신한다.
- 리프레시 토큰: 사용하지 않으면 약 29~30일 후 만료된다고 알려져 있다.
  수집기가 매일 갱신하므로 RPi가 계속 켜져 있으면 만료되지 않는다.
- 토큰 파일은 임시 파일에 쓴 뒤 교체(원자적 저장)하고, 권한은 600이다.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
import time
import urllib.parse
from pathlib import Path

from .config import Config
from .transport import HttpError, request

log = logging.getLogger(__name__)

REFRESH_MARGIN_S = 3600


class ReauthRequired(Exception):
    """사용자가 `stcollector auth` 로 다시 승인해야 하는 상태."""


class TokenStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict | None:
        try:
            return json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return None

    def save(self, token: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # mkstemp 는 0600 권한으로 파일을 만든다.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".token-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(token, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise


class Authenticator:
    def __init__(self, cfg: Config, store: TokenStore, http=request, clock=time.time):
        self.cfg = cfg
        self.store = store
        self.http = http
        self.clock = clock

    # ---- 최초 승인 -------------------------------------------------------
    def authorize_url(self, state: str) -> str:
        query = urllib.parse.urlencode(
            {
                "client_id": self.cfg.client_id,
                "response_type": "code",
                "redirect_uri": self.cfg.redirect_uri,
                "scope": " ".join(self.cfg.scopes),
                "state": state,
            }
        )
        return f"{self.cfg.auth_url}?{query}"

    @staticmethod
    def parse_redirect(text: str, expected_state: str) -> str:
        """브라우저 주소창의 리다이렉트 URL(또는 code 값만)에서 인가 코드를 꺼낸다."""
        text = text.strip()
        if "?" not in text and "=" not in text:
            return text  # code 값만 붙여 넣은 경우
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
        if "error" in qs:
            raise ValueError(f"승인이 거부되었습니다: {qs['error'][0]}")
        if qs.get("state", [None])[0] != expected_state:
            raise ValueError("state 값이 일치하지 않습니다. 방금 출력된 URL로 다시 시도하세요.")
        if "code" not in qs:
            raise ValueError("URL에 code 가 없습니다.")
        return qs["code"][0]

    def exchange_code(self, code: str) -> dict:
        token = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.cfg.redirect_uri,
                "client_id": self.cfg.client_id,
            }
        )
        if not token.get("refresh_token"):
            raise ReauthRequired("응답에 refresh_token 이 없습니다. 앱 설정(OAuth-In, API_ONLY)을 확인하세요.")
        self.store.save(token)
        return token

    # ---- 갱신 -------------------------------------------------------------
    def refresh(self) -> dict:
        current = self.store.load()
        if not current or not current.get("refresh_token"):
            raise ReauthRequired("저장된 토큰이 없습니다. `stcollector auth` 를 실행하세요.")
        token = self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": current["refresh_token"],
                "client_id": self.cfg.client_id,
            }
        )
        if not token.get("refresh_token"):
            token["refresh_token"] = current["refresh_token"]
        self.store.save(token)
        log.info("액세스 토큰 갱신 완료 (만료까지 %.1f시간)", (token["expires_at"] - self.clock()) / 3600)
        return token

    def access_token(self, force_refresh: bool = False) -> str:
        # 매번 파일에서 읽는다: 사용자가 `auth` 로 재승인하면 재시작 없이 반영된다.
        token = self.store.load()
        if not token:
            raise ReauthRequired("저장된 토큰이 없습니다. `stcollector auth` 를 실행하세요.")
        if force_refresh or token["expires_at"] - self.clock() < REFRESH_MARGIN_S:
            token = self.refresh()
        return token["access_token"]

    def status(self) -> dict:
        token = self.store.load()
        if not token:
            return {"state": "missing"}
        now = self.clock()
        return {
            "state": "ok" if token["expires_at"] > now else "expired",
            "expires_in_h": round((token["expires_at"] - now) / 3600, 1),
            "last_obtained_h_ago": round((now - token.get("obtained_at", now)) / 3600, 1),
            "scope": token.get("scope"),
        }

    # ---- 내부 -------------------------------------------------------------
    def _token_request(self, form: dict[str, str]) -> dict:
        basic = base64.b64encode(f"{self.cfg.client_id}:{self.cfg.client_secret}".encode()).decode()
        try:
            resp = self.http(
                "POST", self.cfg.token_url, headers={"Authorization": f"Basic {basic}"}, form=form
            )
        except HttpError as e:
            if e.status in (400, 401):
                raise ReauthRequired(f"토큰 요청 거부 (HTTP {e.status}): {e.body[:200]}") from e
            raise
        data = resp.data or {}
        if "access_token" not in data:
            raise ReauthRequired("토큰 응답에 access_token 이 없습니다.")
        now = self.clock()
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token"),
            "expires_at": now + int(data.get("expires_in", 86400)),
            "obtained_at": now,
            "scope": data.get("scope"),
        }
