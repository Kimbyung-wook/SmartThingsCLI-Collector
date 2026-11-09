"""테스트용 가짜 HTTP와 설정."""

from __future__ import annotations

import tempfile
import urllib.parse
from pathlib import Path

from stcollector.config import DEFAULT_ATTRIBUTES, DEFAULT_STATUS_INTERVALS, Config
from stcollector.transport import HttpError, Response


def make_config(tmp: Path, **overrides) -> Config:
    values = dict(
        client_id="cid",
        client_secret="secret",
        redirect_uri="https://localhost/callback",
        scopes=["r:devices:*", "r:locations:*"],
        location_id="loc-1",
        api_base="https://api.smartthings.com/v1",
        auth_url="https://api.smartthings.com/oauth/authorize",
        token_url="https://api.smartthings.com/oauth/token",
        db_path=tmp / "sensors.db",
        token_path=tmp / "token.json",
        mode="auto",
        history_interval_s=300,
        history_page_limit=200,
        history_max_pages=20,
        history_backfill_max_pages=100,
        backfill_days=7,
        device_refresh_s=86400,
        status_intervals=dict(DEFAULT_STATUS_INTERVALS),
        attributes={k: list(v) for k, v in DEFAULT_ATTRIBUTES.items()},
    )
    values.update(overrides)
    return Config(**values)


class FakeHttp:
    """(method, path) -> 응답 목록. 응답은 dict(JSON) 또는 HttpError."""

    def __init__(self):
        self.routes: dict[tuple[str, str], list] = {}
        self.calls: list[dict] = []

    def add(self, method: str, path: str, *responses):
        self.routes.setdefault((method, path), []).extend(responses)

    def __call__(self, method, url, *, headers=None, params=None, form=None, timeout=30.0):
        parsed = urllib.parse.urlparse(url)
        self.calls.append(
            {"method": method, "url": url, "path": parsed.path, "headers": headers or {},
             "params": params or {}, "form": form, "query": urllib.parse.parse_qs(parsed.query)}
        )
        queue = self.routes.get((method, parsed.path))
        if not queue:
            raise AssertionError(f"예상하지 못한 요청: {method} {url}")
        resp = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(resp, HttpError):
            raise resp
        return Response(200, {}, resp)


class TempDirMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
