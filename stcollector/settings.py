"""집계([rollup])와 대시보드([web]) 설정. 수집기 설정(config.py)과 같은 파일에서 읽는다."""

from __future__ import annotations

import ipaddress
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigError

DAY_S = 86_400

# 각 값을 다음 보고가 없을 때 최대 몇 초까지 유지된 것으로 볼지.
# 이 시간을 넘긴 구간은 "데이터 없음"이 되어 coverage 가 낮아진다.
# 재실·스위치는 상태가 바뀔 때만 보고하므로 길게 둔다.
DEFAULT_MAX_HOLD_S: dict[str, int] = {
    "temperature": 7_200,
    "humidity": 7_200,
    "illuminance": 7_200,
    "power": 7_200,
    "energy": 21_600,
    "motion": 7 * DAY_S,
    "presence": 7 * DAY_S,
    "switch": 365 * DAY_S,
}
FALLBACK_MAX_HOLD_S = 7_200


@dataclass(frozen=True)
class RollupSettings:
    retention_days: int = 365
    max_hold_s: dict[str, int] | None = None

    def hold_ms(self, attribute: str) -> int:
        table = self.max_hold_s if self.max_hold_s is not None else DEFAULT_MAX_HOLD_S
        return table.get(attribute, DEFAULT_MAX_HOLD_S.get(attribute, FALLBACK_MAX_HOLD_S)) * 1000


@dataclass(frozen=True)
class WebSettings:
    host: str
    port: int
    password_hash: str
    allowed_networks: tuple
    session_days: int


@dataclass(frozen=True)
class SysmonSettings:
    interval_s: int = 5
    flush_s: int = 600            # 1분 요약을 몇 초마다 저장할지 (이벤트는 즉시 저장)
    minute_retention_days: int = 14
    event_retention_days: int = 365
    overheat_c: float = 80.0
    runtime_file: str = "/run/stcollector-sysmon/current.json"


def sysmon_db_path(db_path: str | os.PathLike) -> Path:
    """시스템 모니터 DB. 수집 DB 와 쓰기 경합이 없도록 파일을 나눈다."""
    return Path(db_path).parent / "sysmon" / "sysmon.db"


def _read(path: str | os.PathLike) -> dict:
    try:
        return tomllib.loads(Path(path).read_text("utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"설정 파일이 없습니다: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"설정 파일 문법 오류: {e}") from None


def load_rollup_settings(path: str | os.PathLike) -> RollupSettings:
    raw = _read(path).get("rollup", {})
    retention = raw.get("retention_days", 365)
    if not isinstance(retention, int) or isinstance(retention, bool) or retention < 0:
        raise ConfigError(f"rollup.retention_days 는 0 이상의 정수여야 합니다 (현재: {retention!r}, 0은 삭제 안 함)")
    holds = raw.get("max_hold_s", {})
    if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in holds.values()):
        raise ConfigError("rollup.max_hold_s 의 값은 1 이상의 정수(초)여야 합니다")
    return RollupSettings(retention_days=retention, max_hold_s={**DEFAULT_MAX_HOLD_S, **holds})


def load_sysmon_settings(path: str | os.PathLike) -> SysmonSettings:
    raw = _read(path).get("sysmon", {})

    def positive(key: str, default: int, minimum: int) -> int:
        value = raw.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ConfigError(f"sysmon.{key} 는 {minimum} 이상의 정수여야 합니다 (현재: {value!r})")
        return value

    overheat = raw.get("overheat_c", 80.0)
    if isinstance(overheat, bool) or not isinstance(overheat, (int, float)) or not 40 <= overheat <= 100:
        raise ConfigError(f"sysmon.overheat_c 는 40~100 이어야 합니다 (현재: {overheat!r})")
    return SysmonSettings(
        interval_s=positive("interval_s", 5, 1),
        flush_s=positive("flush_s", 600, 60),
        minute_retention_days=positive("minute_retention_days", 14, 1),
        event_retention_days=positive("event_retention_days", 365, 1),
        overheat_c=float(overheat),
        runtime_file=str(raw.get("runtime_file", SysmonSettings.runtime_file)),
    )


def load_web_settings(path: str | os.PathLike) -> WebSettings:
    raw = _read(path).get("web", {})
    port = raw.get("port", 8080)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError(f"web.port 는 1~65535 정수여야 합니다 (현재: {port!r})")
    days = raw.get("session_days", 30)
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
        raise ConfigError(f"web.session_days 는 1~365 정수여야 합니다 (현재: {days!r})")
    try:
        nets = tuple(ipaddress.ip_network(n) for n in raw.get("allowed_networks", ["192.168.0.0/24"]))
    except (TypeError, ValueError) as e:
        raise ConfigError(f"web.allowed_networks 형식 오류: {e}") from None
    return WebSettings(
        host=str(raw.get("host", "0.0.0.0")),
        port=port,
        password_hash=str(raw.get("password_hash", "")),
        allowed_networks=nets,
        session_days=days,
    )
