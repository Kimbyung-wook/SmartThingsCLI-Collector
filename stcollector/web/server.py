"""표준 라이브러리만 쓰는 대시보드 웹서버."""

from __future__ import annotations

import gzip
import hashlib
import http.cookies
import ipaddress
import json
import logging
import mimetypes
import signal
import sqlite3
import threading
import time
import urllib.parse
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..config import ConfigError
from ..db import Database
from ..settings import SysmonSettings, WebSettings, sysmon_db_path
from . import api
from .session import LoginLimiter, Sessions, valid_hash, verify_password

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).with_name("static")
COOKIE = "st_session"
MAX_BODY = 4096
MAX_CONNECTIONS = 32
CACHE_ENTRIES = 64
LIVE_CACHE_MS = 15_000
# 로그인 전에 받아야 하는 파일 (스타일, 홈 화면 아이콘, manifest)
PUBLIC_STATIC = {"/static/style.css", "/manifest.webmanifest"}
# SmartThings 허브의 Edge 드라이버가 읽는 경로 (로그인 없음, 허용 주소만)
HUB_PATH = "/hub/status"

LOGIN_MESSAGES = {  # 고정 문구만 넣는다 (요청 값을 페이지에 쓰지 않는다)
    "bad": "비밀번호가 맞지 않습니다.",
    "locked": "실패가 많아 5분간 로그인이 막혔습니다.",
}

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
        "manifest-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # no-referrer 로 두면 브라우저가 같은 출처의 POST 에도 "Origin: null" 을 보내 로그인이 거부된다.
    "Referrer-Policy": "same-origin",
}

mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("text/javascript", ".js")


class StaticFile:
    __slots__ = ("body", "gz", "ctype", "etag")

    def __init__(self, path: Path):
        self.body = path.read_bytes()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.ctype = ctype + ("; charset=utf-8" if ctype.startswith("text/") or "json" in ctype else "")
        self.gz = gzip.compress(self.body, 6, mtime=0) if not path.suffix == ".png" else None
        self.etag = '"' + hashlib.sha256(self.body).hexdigest()[:16] + '"'


def load_static(root: Path = STATIC_DIR) -> dict[str, StaticFile]:
    """시작할 때 파일을 메모리에 올린다. 요청 경로는 이 표에서만 찾으므로 경로 탈출이 불가능하다."""
    files = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            files["/static/" + p.relative_to(root).as_posix()] = StaticFile(p)
    files["/"] = files["/static/index.html"]
    files["/login"] = files["/static/login.html"]
    files["/manifest.webmanifest"] = files["/static/manifest.webmanifest"]
    return files


class App:
    def __init__(self, db_path: Path, settings: WebSettings, clock=time.time,
                 sysmon_db: Path | None = None, sysmon_current: Path | None = None):
        if not settings.password_hash:
            raise ConfigError(
                "대시보드 비밀번호가 설정되지 않아 웹서버를 시작하지 않습니다. "
                "`stcollector hash-password` 로 해시를 만들어 설정 파일의 [web] password_hash 에 넣으세요."
            )
        if not valid_hash(settings.password_hash):
            raise ConfigError("web.password_hash 형식이 올바르지 않습니다. `stcollector hash-password` 결과를 그대로 넣으세요.")
        self.db_path = Path(db_path)
        self.settings = settings
        self.clock = clock
        self.sessions = Sessions(settings.password_hash, settings.session_days, clock=clock)
        self.limiter = LoginLimiter(clock=clock)
        self.static = load_static()
        self._cache: OrderedDict[str, tuple] = OrderedDict()
        self._cache_lock = threading.Lock()
        Database(self.db_path).close()  # 스키마 적용 (이후 요청은 읽기 전용 연결만 쓴다)
        self.db_uri = self.db_path.resolve().as_uri() + "?mode=ro"
        self.sysmon_db = Path(sysmon_db) if sysmon_db else sysmon_db_path(self.db_path)
        self.sysmon_current = Path(sysmon_current or SysmonSettings.runtime_file)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_uri, uri=True, timeout=5)
        conn.execute("PRAGMA cache_size=-2048")
        return conn

    def allowed_ip(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if getattr(addr, "ipv4_mapped", None):
            addr = addr.ipv4_mapped
        return addr.is_loopback or any(addr in net for net in self.settings.allowed_networks)

    def hub_allowed(self, ip: str) -> bool:
        """/hub/status 를 읽을 수 있는 주소인지. 목록에 적힌 주소만 허용한다 (루프백도 따로 적어야 함)."""
        if not self.settings.hub_status_allow:
            return False
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if getattr(addr, "ipv4_mapped", None):
            addr = addr.ipv4_mapped
        return addr in self.settings.hub_status_allow

    def cached(self, key: str, version: str, build) -> tuple[bytes, bytes]:
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and hit[0] == version:
                self._cache.move_to_end(key)
                return hit[1], hit[2]
        body = json.dumps(build(), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        gz = gzip.compress(body, 5, mtime=0)
        with self._cache_lock:
            self._cache[key] = (version, body, gz)
            self._cache.move_to_end(key)
            while len(self._cache) > CACHE_ENTRIES:
                self._cache.popitem(last=False)
        return body, gz


class Handler(BaseHTTPRequestHandler):
    server_version = "stcollector"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30

    @property
    def app(self) -> App:
        return self.server.app

    def log_message(self, fmt, *args):  # 접속 로그를 남기지 않는다 (microSD 쓰기 절약)
        log.debug("%s " + fmt, self.client_address[0], *args)

    # ---- 응답 --------------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str, *, gz: bytes | None = None,
              headers: dict | None = None, head_only: bool = False):
        encoded = gz is not None and "gzip" in self.headers.get("Accept-Encoding", "")
        payload = gz if encoded else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        if encoded:
            self.send_header("Content-Encoding", "gzip")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head_only:
            self.wfile.write(payload)

    def _json_error(self, status: int, message: str):
        body = json.dumps({"error": message}, ensure_ascii=False).encode()
        self._send(status, body, "application/json; charset=utf-8", headers={"Cache-Control": "no-store"})

    def _redirect(self, location: str, headers: dict | None = None):
        self._send(303, b"", "text/plain; charset=utf-8", headers={"Location": location, **(headers or {})})

    def _text(self, status: int, message: str):
        self._send(status, message.encode(), "text/plain; charset=utf-8", headers={"Cache-Control": "no-store"})

    def _cookie_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = http.cookies.SimpleCookie(raw)
        except http.cookies.CookieError:
            return None
        return jar[COOKIE].value if COOKIE in jar else None

    def _authed(self) -> bool:
        return self.app.sessions.valid(self._cookie_token())

    def _cookie_header(self, token: str, max_age: int) -> str:
        # 집 안 HTTP 전용이라 Secure 는 붙일 수 없다.
        return f"{COOKIE}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"

    def _ip_ok(self) -> bool:
        if self.app.allowed_ip(self.client_address[0]):
            return True
        self._text(403, "허용되지 않은 네트워크입니다")
        return False

    # ---- 라우팅 --------------------------------------------------------------
    def do_HEAD(self):
        self.do_GET(head_only=True)

    def do_GET(self, head_only: bool = False):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == HUB_PATH:
            self._hub_status(head_only)
            return
        if not self._ip_ok():
            return
        if path.startswith("/api/"):
            self._api(path, parsed.query, head_only)
            return
        file = self.app.static.get(path)
        if file is None:
            self._text(404, "없는 주소입니다")
            return
        public = path == "/login" or path in PUBLIC_STATIC or path.startswith("/static/icons/")
        if not public and not self._authed():
            if path == "/":
                self._redirect("/login")
            else:
                self._text(401, "로그인이 필요합니다")
            return
        if path == "/login":
            self._login_page(parsed.query, file, head_only)
            return
        if self.headers.get("If-None-Match") == file.etag:
            self._send(304, b"", file.ctype, headers={"ETag": file.etag, "Cache-Control": "no-cache"}, head_only=True)
            return
        self._send(200, file.body, file.ctype, gz=file.gz,
                   headers={"ETag": file.etag, "Cache-Control": "no-cache"}, head_only=head_only)

    def _login_page(self, query: str, file: StaticFile, head_only: bool):
        code = dict(urllib.parse.parse_qsl(query)).get("e")
        message = LOGIN_MESSAGES.get(code)
        html = file.body
        if message:
            html = html.replace(b"<!--MESSAGE-->", f'<p class="login-msg" role="alert">{message}</p>'.encode())
        self._send(200, html, file.ctype, headers={"Cache-Control": "no-store"}, head_only=head_only)

    def do_POST(self):
        if not self._ip_ok():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path not in ("/login", "/logout"):
            self._text(404, "없는 주소입니다")
            return
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
            self._text(403, "다른 출처의 요청은 받지 않습니다")
            return
        if path == "/logout":
            self.app.sessions.revoke(self._cookie_token())
            self._redirect("/login", {"Set-Cookie": self._cookie_header("", 0)})
            return
        self._login()

    def _login(self):
        ip = self.client_address[0]
        app = self.app
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._text(411, "Content-Length 가 필요합니다")
            return
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            self._text(413, "요청이 너무 깁니다")
            return
        raw = self.rfile.read(length).decode("utf-8", "replace")
        if app.limiter.blocked(ip):
            self._redirect("/login?e=locked")
            return
        password = urllib.parse.parse_qs(raw, keep_blank_values=True).get("password", [""])[0]
        with app.limiter.verify_lock:
            ok = bool(password) and verify_password(password, app.settings.password_hash)
        if not ok:
            app.limiter.fail(ip)
            self._redirect("/login?e=bad")
            return
        app.limiter.success(ip)
        self._redirect("/", {"Set-Cookie": self._cookie_header(app.sessions.create(), app.sessions.max_age)})

    def _hub_status(self, head_only: bool):
        """SmartThings 허브용 읽기 전용 요약. 로그인 없이 hub_status_allow 의 주소에서만 열린다.

        목록이 비었거나 다른 주소면 경로가 없는 것처럼 404 를 돌려준다.
        """
        app = self.app
        if not app.hub_allowed(self.client_address[0]):
            self._text(404, "없는 주소입니다")
            return
        conn = app.connect()
        try:
            ctx = api.Context(conn, int(app.clock() * 1000), app.db_path, app.sysmon_db, app.sysmon_current)
            data = api.hub_status(ctx)
        except sqlite3.Error:
            log.exception("DB 조회 실패: %s", HUB_PATH)
            self._json_error(503, "데이터를 읽지 못했습니다")
            return
        finally:
            conn.close()
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        self._send(200, body, "application/json; charset=utf-8", headers={"Cache-Control": "no-store"},
                   head_only=head_only)

    def _api(self, path: str, query: str, head_only: bool):
        if not self._authed():
            self._json_error(401, "로그인이 필요합니다")
            return
        handler = api.ROLLUP_ROUTES.get(path)
        live = False
        if handler is None:
            handler = api.LIVE_ROUTES.get(path)
            live = handler is not None
        if handler is None:
            self._json_error(404, "없는 API입니다")
            return
        params = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
        app = self.app
        now_ms = int(app.clock() * 1000)
        conn = app.connect()
        try:
            ctx = api.Context(conn, now_ms, app.db_path, app.sysmon_db, app.sysmon_current)
            if live:
                version = str(now_ms // LIVE_CACHE_MS)
            else:
                version = f"{ctx.meta('rollup_finished_ms')}:{now_ms // api.HOUR_MS}"
            key = f"{path}?{urllib.parse.urlencode(sorted(params.items()))}"
            body, gz = app.cached(key, version, lambda: handler(ctx, params))
        except api.BadRequest as e:
            self._json_error(400, str(e))
            return
        except sqlite3.Error:
            log.exception("DB 조회 실패: %s", path)
            self._json_error(503, "데이터를 읽지 못했습니다")
            return
        finally:
            conn.close()
        self._send(200, body, "application/json; charset=utf-8", gz=gz,
                   headers={"Cache-Control": "no-store"}, head_only=head_only)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, app: App):
        super().__init__(address, Handler)
        self.app = app
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        log.debug("요청 처리 중 연결 오류", exc_info=True)


def serve(db_path: Path, settings: WebSettings, sysmon_current: str | None = None) -> int:
    app = App(db_path, settings, sysmon_current=Path(sysmon_current) if sysmon_current else None)
    server = Server((settings.host, settings.port), app)

    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("대시보드 시작: http://%s:%d", settings.host, settings.port)
    server.serve_forever()
    server.server_close()
    log.info("대시보드 종료")
    return 0
