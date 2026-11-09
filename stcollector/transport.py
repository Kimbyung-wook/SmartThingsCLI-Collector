"""urllib 기반 최소 HTTPS/JSON 전송 계층 (외부 라이브러리 없음).

보안상 두 가지를 강제한다.
- https:// 가 아닌 URL은 거부한다.
- HTTP 리다이렉트를 따라가지 않는다. 따라가면 Authorization 헤더가 다른 호스트로 넘어갈 수 있다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import __version__

USER_AGENT = f"stcollector/{__version__}"


class TransportError(Exception):
    """네트워크 수준 오류 (연결 실패, 타임아웃 등)."""


class HttpError(Exception):
    def __init__(self, status: int, body: str, headers: dict[str, str] | None = None):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body
        self.headers = headers or {}


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    data: object


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # 리다이렉트를 따라가지 않으면 HTTPError(3xx)가 발생한다


_opener = urllib.request.build_opener(_NoRedirect())


def _lower(headers) -> dict[str, str]:
    return {k.lower(): v for k, v in (headers.items() if headers else [])}


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, object] | None = None,
    form: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> Response:
    if not url.startswith("https://"):
        raise TransportError(f"HTTPS가 아닌 URL은 허용하지 않습니다: {url}")
    if params:
        query = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}, doseq=True
        )
        url = f"{url}{'&' if '?' in url else '?'}{query}"

    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        h.update(headers)
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form).encode("utf-8")
        h["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
            resp_headers = _lower(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read() if e.fp else b""
        raise HttpError(e.code, raw.decode("utf-8", "replace"), _lower(e.headers)) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TransportError(str(e)) from e

    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError as e:
        raise TransportError(f"JSON이 아닌 응답: {raw[:200]!r}") from e
    return Response(status, resp_headers, data)
