"""SmartThings REST API 클라이언트 (읽기 전용)."""

from __future__ import annotations

import logging
import time
import urllib.parse
from collections.abc import Iterator

from .auth import Authenticator
from .config import Config
from .transport import HttpError, TransportError, request

log = logging.getLogger(__name__)


def _retry_after(headers: dict[str, str]) -> float | None:
    value = headers.get("retry-after")
    try:
        return max(1.0, float(value)) if value is not None else None
    except ValueError:
        return None


class SmartThingsClient:
    def __init__(
        self,
        cfg: Config,
        auth: Authenticator,
        http=request,
        sleep=time.sleep,
        max_retries: int = 3,
    ):
        self.base = cfg.api_base.rstrip("/")
        parsed = urllib.parse.urlparse(self.base)
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        self._host = parsed.netloc
        self.auth = auth
        self.http = http
        self.sleep = sleep
        self.max_retries = max_retries

    def _url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("https://", "http://")):
            # 페이지 링크(_links.next.href)를 따라갈 때 토큰이 다른 호스트로 가지 않게 막는다.
            if urllib.parse.urlparse(path_or_url).netloc != self._host or not path_or_url.startswith("https://"):
                raise ValueError(f"다른 호스트로는 토큰을 보내지 않습니다: {path_or_url}")
            return path_or_url
        if path_or_url.startswith("/"):
            return f"{self._origin}{path_or_url}"
        return f"{self.base}/{path_or_url}"

    def get(self, path_or_url: str, params: dict | None = None) -> dict:
        url = self._url(path_or_url)
        refreshed = False
        attempt = 0
        while True:
            token = self.auth.access_token()
            try:
                resp = self.http("GET", url, headers={"Authorization": f"Bearer {token}"}, params=params)
                return resp.data or {}
            except HttpError as e:
                if e.status == 401 and not refreshed:
                    log.info("401 응답: 토큰을 갱신하고 다시 시도합니다")
                    self.auth.access_token(force_refresh=True)
                    refreshed = True
                    continue
                if (e.status == 429 or e.status >= 500) and attempt < self.max_retries:
                    delay = _retry_after(e.headers) or min(60.0, 5.0 * 2**attempt)
                    log.warning("HTTP %s, %.0f초 후 재시도 (%d/%d)", e.status, delay, attempt + 1, self.max_retries)
                    attempt += 1
                    self.sleep(delay)
                    continue
                raise
            except TransportError as e:
                if attempt < self.max_retries:
                    delay = min(60.0, 5.0 * 2**attempt)
                    log.warning("네트워크 오류 (%s), %.0f초 후 재시도", e, delay)
                    attempt += 1
                    self.sleep(delay)
                    continue
                raise

    def paged(self, path: str, params: dict | None = None) -> Iterator[list[dict]]:
        """페이지 단위로 items 목록을 내보낸다. _links.next.href 를 따라간다."""
        data = self.get(path, params)
        while True:
            yield data.get("items", [])
            nxt = ((data.get("_links") or {}).get("next") or {}).get("href")
            if not nxt:
                return
            data = self.get(nxt)

    # ---- 엔드포인트 -------------------------------------------------------
    def locations(self) -> list[dict]:
        return [item for page in self.paged("locations") for item in page]

    def rooms(self, location_id: str) -> list[dict]:
        return [item for page in self.paged(f"locations/{location_id}/rooms") for item in page]

    def devices(self, location_id: str) -> list[dict]:
        return [item for page in self.paged("devices", {"locationId": location_id}) for item in page]

    def device_status(self, device_id: str) -> dict:
        return self.get(f"devices/{device_id}/status")

    def device_history_pages(
        self, location_id: str, device_id: str, limit: int = 200
    ) -> Iterator[list[dict]]:
        """기기 이벤트 이력. 최신 이벤트부터 과거 방향으로 페이지를 내보낸다.

        공식 문서에는 없고 SmartThings Core SDK(src/endpoint/history.ts)에서 확인한 형식이다.
        """
        return self.paged(
            "history/devices",
            {
                "locationId": location_id,
                "deviceId": device_id,
                "limit": limit,
                "oldestFirst": "false",
            },
        )
