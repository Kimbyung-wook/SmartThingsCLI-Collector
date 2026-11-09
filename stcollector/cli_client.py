"""SmartThings CLI를 실행해 읽는 클라이언트 (`SmartThingsClient` 대체).

CLI 자격증명의 권한은 `controller:stCli` 로 읽기 전용이 아니다. 그래서 이 모듈은
아래 ALLOWED_COMMANDS 에 있는 읽기 명령만 실행한다. 장치 제어 명령을 추가하지 마세요.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .auth import ReauthRequired
from .config import Config
from .transport import HttpError, TransportError

log = logging.getLogger(__name__)

CREDENTIALS_RELATIVE = Path(".local/share/@smartthings/cli/credentials.json")
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"
TERMINATE_GRACE_S = 5.0
RETRY_DELAY_S = 30.0
MAX_ERROR_CHARS = 300
# RPi 실측: --after 를 주면 --limit 200, 500 이어도 100건까지만 돌려준다.
MAX_HISTORY_PAGE = 100


class CliCommandNotAllowed(ValueError):
    """허용 목록에 없는 CLI 명령이나 인자를 실행하려 한 경우."""


class CliError(TransportError):
    """CLI 실행 실패 (시간 초과, 비정상 종료, JSON이 아닌 출력 등)."""


@dataclass(frozen=True)
class _CommandSpec:
    positional: int = 0
    options: frozenset[str] = frozenset()


_HISTORY_OPTIONS = frozenset({"--limit", "--after", "--before"})

# 실행할 수 있는 명령 전체. 읽기 명령만 넣는다.
ALLOWED_COMMANDS: Mapping[str, _CommandSpec] = {
    "locations": _CommandSpec(),
    "locations:rooms": _CommandSpec(options=frozenset({"--location"})),
    "locations:history": _CommandSpec(positional=1, options=_HISTORY_OPTIONS),
    "devices": _CommandSpec(options=frozenset({"--location", "--status"})),
    "devices:status": _CommandSpec(positional=1),
    "devices:history": _CommandSpec(positional=1, options=_HISTORY_OPTIONS),
}

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_OPTION_VALUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:.+_-]*\Z")


def build_argv(
    cli_path: str,
    command: str,
    positional: Sequence[str] = (),
    options: Mapping[str, object | None] | None = None,
) -> list[str]:
    """허용 목록을 검사해 인자 배열을 만든다. 벗어나면 CliCommandNotAllowed."""
    spec = ALLOWED_COMMANDS.get(command)
    if spec is None:
        raise CliCommandNotAllowed(f"허용되지 않은 CLI 명령입니다: {command!r}")
    if len(positional) != spec.positional or not all(
        isinstance(p, str) and _ID_RE.match(p) for p in positional
    ):
        raise CliCommandNotAllowed(f"{command}: 위치 인자가 허용 형식이 아닙니다")

    argv = [cli_path, command, *positional, "--json"]
    for name, value in (options or {}).items():
        if name not in spec.options:
            raise CliCommandNotAllowed(f"{command}: 허용되지 않은 옵션입니다: {name!r}")
        argv.append(name)
        if value is not None:
            text = str(value)
            if not _OPTION_VALUE_RE.match(text):
                raise CliCommandNotAllowed(f"{command}: {name} 값이 허용 형식이 아닙니다")
            argv.append(text)
    return argv


# ---- 오류 문구 정리 ----------------------------------------------------------
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[^\s\"',;]+"),
    re.compile(
        r"(?i)[\"']?\b(?:access_?token|refresh_?token|id_?token|token|client_?secret|secret|"
        r"password|authorization|code)\b[\"']?\s*[:=]\s*[\"']?[^\s\"',;&}]+"
    ),
    re.compile(r"\beyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*"),
    re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
    re.compile(r"\b[A-Za-z0-9+/_-]{32,}={0,2}"),
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def sanitize(text: str) -> str:
    """로그와 예외에 넣어도 되도록 토큰으로 보이는 값을 가리고 길이를 줄인다."""
    cleaned = _ANSI_RE.sub("", text or "")
    # 잘라 낸 뒤 가리면 토큰 조각이 남을 수 있으므로 먼저 전체를 가린다.
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("[가림]", cleaned)
    return _CONTROL_RE.sub(" ", cleaned).strip()[:MAX_ERROR_CHARS]


_REAUTH_RE = re.compile(
    r"(?i)\b401\b|unauthori[sz]ed|invalid[_ ]grant|localhost:61973|"
    r"(?:token|session|credentials?)\b.{0,30}\b(?:expired|invalid|revoked)|"
    r"\b(?:expired|invalid|revoked)\b.{0,30}\b(?:token|session|credentials?)\b|"
    r"not (?:logged|signed) ?in|(?:please|must|need to) (?:log|sign) ?in|"
    r"log ?in (?:required|again)|re-?authenticat"
)
_HTTP_STATUS_RE = re.compile(r"(?i)(?:status(?: code)?|HTTP(?:/\d(?:\.\d)?)?)[\s:=]+([1-5]\d\d)\b")


def _looks_like_reauth(text: str) -> bool:
    return bool(_REAUTH_RE.search(text))


def _http_status(text: str) -> int | None:
    match = _HTTP_STATUS_RE.search(text)
    return int(match.group(1)) if match else None


def _to_iso(ms: int) -> str:
    moment = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=ms)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _items(data: object) -> list[dict]:
    if isinstance(data, list):
        return data
    raise CliError("CLI 출력이 목록 형식이 아닙니다")


# ---- 프로세스 실행 -----------------------------------------------------------
Runner = Callable[[Sequence[str], Mapping[str, str], float], tuple[int, str, str]]


def _terminate(proc: subprocess.Popen) -> tuple[str, str]:
    """프로세스 그룹을 끝내고 남은 출력을 돌려준다. SIGTERM 후 안 끝나면 SIGKILL."""

    def send(sig_name: str) -> None:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, getattr(signal, sig_name))
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError):
            pass

    send("SIGTERM")
    try:
        return proc.communicate(timeout=TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired:
        send("SIGKILL")
        return proc.communicate()


def run_cli(argv: Sequence[str], env: Mapping[str, str], timeout_s: float) -> tuple[int, str, str]:
    """CLI를 한 번 실행해 (종료 코드, stdout, stderr)를 돌려준다. 셸을 쓰지 않는다."""
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        cwd=env.get("HOME"),
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=os.name == "posix",  # 시간 초과 때 자식까지 그룹으로 끝낸다
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        stdout, stderr = _terminate(proc)
        raise subprocess.TimeoutExpired(argv, timeout_s, output=stdout, stderr=stderr) from None
    except BaseException:
        _terminate(proc)
        raise
    return proc.returncode, stdout, stderr


# ---- 클라이언트 --------------------------------------------------------------
class SmartThingsCliClient:
    def __init__(self, cfg: Config, runner: Runner = run_cli, sleep=time.sleep, max_retries: int = 1):
        self.cli_path = cfg.cli_path
        self.home = cfg.cli_home
        self.timeout_s = cfg.cli_timeout_s
        self.credentials_path = Path(cfg.cli_home) / CREDENTIALS_RELATIVE
        self.runner = runner
        self.sleep = sleep
        self.max_retries = max_retries
        self._env = {
            "HOME": cfg.cli_home,
            "PATH": SAFE_PATH,
            "LANG": "C.UTF-8",
            "NO_COLOR": "1",
        }

    def _check_logged_in(self) -> None:
        try:
            self.credentials_path.stat()
        except FileNotFoundError:
            # 로그인 없이 실행하면 CLI가 브라우저 로그인을 기다리다 시간 초과된다.
            raise ReauthRequired("SmartThings CLI 로그인 정보가 없습니다.") from None
        except OSError:
            pass  # 권한 문제 등은 CLI 실행 결과로 보고된다

    def _run_once(self, command: str, argv: list[str]) -> object:
        try:
            code, stdout, stderr = self.runner(argv, self._env, self.timeout_s)
        except subprocess.TimeoutExpired as e:
            partial = f"{e.stderr or ''}\n{e.output or ''}"
            if _looks_like_reauth(partial):
                raise ReauthRequired("SmartThings CLI 로그인이 만료된 것으로 보입니다.") from None
            raise CliError(f"{command}: {self.timeout_s}초 안에 끝나지 않아 종료했습니다") from None
        except OSError as e:
            raise CliError(f"{command}: CLI를 실행할 수 없습니다 ({type(e).__name__})") from None

        if code != 0:
            combined = f"{stderr}\n{stdout}"
            if _looks_like_reauth(combined):
                raise ReauthRequired("SmartThings CLI 로그인이 만료된 것으로 보입니다.")
            message = sanitize(stderr or stdout)
            status = _http_status(combined)
            if status is not None:
                raise HttpError(status, f"{command}: {message}")
            raise CliError(f"{command}: 종료 코드 {code}: {message}")

        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            if _looks_like_reauth(f"{stderr}\n{stdout}"):
                raise ReauthRequired("SmartThings CLI 로그인이 만료된 것으로 보입니다.") from None
            raise CliError(f"{command}: JSON이 아닌 출력을 받았습니다") from None

    def _run(
        self,
        command: str,
        positional: Sequence[str] = (),
        options: Mapping[str, object | None] | None = None,
    ) -> object:
        argv = build_argv(self.cli_path, command, positional, options)
        self._check_logged_in()
        attempt = 0
        while True:
            try:
                return self._run_once(command, argv)
            except HttpError as e:
                if not (e.status == 429 or e.status >= 500) or attempt >= self.max_retries:
                    raise
                attempt += 1
                log.warning("%s: HTTP %s, %.0f초 후 재시도", command, e.status, RETRY_DELAY_S)
                self.sleep(RETRY_DELAY_S)

    def _history_pages(
        self, command: str, target: str, limit: int, after_ms: int | None
    ) -> Iterator[list[dict]]:
        limit = min(limit, MAX_HISTORY_PAGE)
        if after_ms is None:
            return self._history_pages_backward(command, target, limit)
        return self._history_pages_forward(command, target, limit, after_ms)

    def _history_pages_forward(
        self, command: str, target: str, limit: int, after_ms: int
    ) -> Iterator[list[dict]]:
        """after_ms 이후를 과거→최신 방향으로 페이지 단위로 내보낸다.

        RPi 실측: --after 를 주면 그 시각 바로 다음의 가장 오래된 이벤트부터 limit 건을 준다
        (페이지 안의 정렬만 최신순). --before 를 함께 줘도 그 범위 안에서만 잘릴 뿐이다.
        그래서 --after 를 앞으로 옮겨 가며 읽는다. 도움말과 달리 경계 시각은 포함되지 않았다.
        """
        after = after_ms
        while True:
            page = _items(self._run(command, (target,), {"--limit": limit, "--after": _to_iso(after)}))
            yield page
            if len(page) < limit:
                return
            newest = max(int(item["epoch"]) for item in page)
            # 경계와 같은 시각의 이벤트를 놓치지 않도록 1ms 겹친다. 중복은 DB가 걸러낸다.
            after = max(newest - 1, after + 1)

    def _history_pages_backward(self, command: str, target: str, limit: int) -> Iterator[list[dict]]:
        """--after 없이 최신→과거 방향으로 --before 를 옮겨 가며 내보낸다."""
        before_ms: int | None = None
        while True:
            options: dict[str, object] = {"--limit": limit}
            if before_ms is not None:
                options["--before"] = _to_iso(before_ms)
            page = _items(self._run(command, (target,), options))
            yield page
            if len(page) < limit:
                return

            oldest = min(int(item["epoch"]) for item in page)
            # --before 는 경계를 포함하지 않으므로 같은 시각의 이벤트를 위해 1ms 겹친다.
            next_before = oldest + 1
            if before_ms is not None and next_before >= before_ms:
                next_before = oldest  # 같은 시각의 이벤트가 한 페이지를 채운 경우
                if next_before >= before_ms:
                    log.warning("%s: 페이지가 더 진행되지 않아 이력 조회를 멈춥니다", command)
                    return
            before_ms = next_before

    # ---- 엔드포인트 -------------------------------------------------------
    def locations(self) -> list[dict]:
        return _items(self._run("locations"))

    def rooms(self, location_id: str) -> list[dict]:
        return _items(self._run("locations:rooms", options={"--location": location_id}))

    def devices(self, location_id: str) -> list[dict]:
        return _items(self._run("devices", options={"--location": location_id}))

    def device_status(self, device_id: str) -> dict:
        data = self._run("devices:status", (device_id,))
        if not isinstance(data, dict):
            raise CliError("devices:status: CLI 출력이 객체 형식이 아닙니다")
        return data

    def device_statuses(self, location_id: str, device_ids: Sequence[str]) -> dict[str, dict]:
        """기기 상태를 `devices --status` 1회로 읽어 `devices:status` 와 같은 형태로 돌려준다."""
        statuses: dict[str, dict] = {}
        wanted = set(device_ids)
        options = {"--location": location_id, "--status": None}
        for device in _items(self._run("devices", options=options)):
            device_id = device.get("deviceId")
            if device_id not in wanted:
                continue
            components: dict[str, dict] = {}
            for comp in device.get("components") or []:
                caps = {
                    cap["id"]: cap["status"]
                    for cap in comp.get("capabilities") or []
                    if cap.get("id") and isinstance(cap.get("status"), dict)
                }
                if caps:
                    components[comp.get("id") or "main"] = caps
            if components:
                statuses[device_id] = {"components": components}
        return statuses

    def device_history_pages(
        self, location_id: str, device_id: str, limit: int = 200, after_ms: int | None = None
    ) -> Iterator[list[dict]]:
        return self._history_pages("devices:history", device_id, limit, after_ms)

    def location_history_pages(
        self, location_id: str, limit: int = 200, after_ms: int | None = None
    ) -> Iterator[list[dict]]:
        """위치 전체의 이벤트 이력. 기기별 호출보다 CLI 실행 횟수가 훨씬 적다."""
        return self._history_pages("locations:history", location_id, limit, after_ms)
