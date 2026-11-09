"""설정 파일(TOML) 로드와 검증."""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# 수집할 (capability -> attributes). 설정 파일에서 덮어쓸 수 있다.
DEFAULT_ATTRIBUTES: dict[str, list[str]] = {
    "temperatureMeasurement": ["temperature"],
    "relativeHumidityMeasurement": ["humidity"],
    "illuminanceMeasurement": ["illuminance"],
    "motionSensor": ["motion"],
    "presenceSensor": ["presence"],
    "switch": ["switch"],
    "powerMeter": ["power"],
    "energyMeter": ["energy"],
    "battery": ["battery"],
}

# 상태 조회(status) 방식일 때 capability별 조회 주기(초).
# 한 기기는 자신이 가진 capability 중 가장 짧은 주기로 조회된다.
DEFAULT_STATUS_INTERVALS: dict[str, int] = {
    "temperatureMeasurement": 300,
    "relativeHumidityMeasurement": 300,
    "illuminanceMeasurement": 60,
    "motionSensor": 30,
    "presenceSensor": 30,
    "switch": 60,
    "powerMeter": 60,
    "energyMeter": 900,
    "battery": 21600,
}

MODES = ("auto", "history", "status")
BACKENDS = ("oauth", "cli")
DEFAULT_CLI_PATH = "/opt/stcollector-cli/smartthings"
DEFAULT_CLI_HOME = "/var/lib/stcollector/cli"
DEFAULT_CLI_TIMEOUT_S = 120


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: list[str]
    location_id: str | None
    api_base: str
    auth_url: str
    token_url: str
    db_path: Path
    token_path: Path
    mode: str
    history_interval_s: int
    history_page_limit: int
    history_max_pages: int
    history_backfill_max_pages: int
    backfill_days: int
    device_refresh_s: int
    status_intervals: dict[str, int] = field(default_factory=dict)
    attributes: dict[str, list[str]] = field(default_factory=dict)
    log_level: str = "INFO"
    source_path: Path | None = None
    backend: str = "oauth"
    cli_path: str = DEFAULT_CLI_PATH
    cli_home: str = DEFAULT_CLI_HOME
    cli_timeout_s: int = DEFAULT_CLI_TIMEOUT_S

    def require_credentials(self) -> None:
        if not self.client_id or not self.client_secret:
            raise ConfigError(
                "client_id / client_secret 이 비어 있습니다. "
                "SmartThings CLI로 만든 앱의 값을 설정 파일에 넣으세요."
            )


def _int(section: dict, key: str, default: int, minimum: int = 1) -> int:
    value = section.get(key, default)
    if not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{key} 는 {minimum} 이상의 정수여야 합니다 (현재: {value!r})")
    return value


def _absolute_path(section: dict, key: str, default: str) -> str:
    """CLI 경로는 상대 경로를 허용하지 않는다 (HOME과 실행 파일이 작업 폴더에 좌우되면 안 된다)."""
    value = section.get(key, default)
    if not isinstance(value, str) or not (value.startswith("/") or os.path.isabs(value)):
        raise ConfigError(f"smartthings.{key} 는 절대 경로 문자열이어야 합니다 (현재: {value!r})")
    return value


def load_config(path: str | os.PathLike) -> Config:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"설정 파일이 없습니다: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"설정 파일 문법 오류: {e}") from None

    st = raw.get("smartthings", {})
    paths = raw.get("paths", {})
    col = raw.get("collect", {})
    lg = raw.get("log", {})
    base_dir = path.parent

    def resolve(value: str | None, default: str) -> Path:
        p = Path(value or default)
        return p if p.is_absolute() else (base_dir / p).resolve()

    mode = col.get("mode", "auto")
    if mode not in MODES:
        raise ConfigError(f"collect.mode 는 {MODES} 중 하나여야 합니다 (현재: {mode!r})")

    attributes = col.get("attributes") or DEFAULT_ATTRIBUTES
    if not all(isinstance(v, list) for v in attributes.values()):
        raise ConfigError("collect.attributes 의 값은 속성 이름 목록이어야 합니다")

    status_intervals = {**DEFAULT_STATUS_INTERVALS, **col.get("status_interval_s", {})}

    backend = st.get("backend", "oauth")
    if backend not in BACKENDS:
        raise ConfigError(f"smartthings.backend 는 {BACKENDS} 중 하나여야 합니다 (현재: {backend!r})")

    api_base = st.get("api_base", "https://api.smartthings.com/v1").rstrip("/")
    auth_url = st.get("auth_url", "https://api.smartthings.com/oauth/authorize")
    token_url = st.get("token_url", "https://api.smartthings.com/oauth/token")
    for name, url in (("api_base", api_base), ("auth_url", auth_url), ("token_url", token_url)):
        if not url.startswith("https://"):
            raise ConfigError(f"{name} 는 https:// 로 시작해야 합니다")

    return Config(
        client_id=st.get("client_id", ""),
        client_secret=st.get("client_secret", ""),
        redirect_uri=st.get("redirect_uri", "https://localhost/callback"),
        scopes=list(st.get("scopes", ["r:devices:*", "r:locations:*"])),
        location_id=st.get("location_id") or None,
        api_base=api_base,
        auth_url=auth_url,
        token_url=token_url,
        db_path=resolve(paths.get("db"), "data/sensors.db"),
        token_path=resolve(paths.get("token"), "data/token.json"),
        mode=mode,
        history_interval_s=_int(col, "history_interval_s", 300, minimum=30),
        history_page_limit=_int(col, "history_page_limit", 200),
        history_max_pages=_int(col, "history_max_pages", 20),
        history_backfill_max_pages=_int(col, "history_backfill_max_pages", 100),
        backfill_days=_int(col, "backfill_days", 7, minimum=0),
        device_refresh_s=_int(col, "device_refresh_s", 86400, minimum=300),
        status_intervals=status_intervals,
        attributes={k: list(v) for k, v in attributes.items()},
        log_level=str(lg.get("level", "INFO")).upper(),
        source_path=path,
        backend=backend,
        cli_path=_absolute_path(st, "cli_path", DEFAULT_CLI_PATH),
        cli_home=_absolute_path(st, "cli_home", DEFAULT_CLI_HOME),
        cli_timeout_s=_int(st, "cli_timeout_s", DEFAULT_CLI_TIMEOUT_S),
    )


def permission_warning(path: Path) -> str | None:
    """비밀값이 든 파일을 다른 사용자가 읽을 수 있으면 경고 문구를 돌려준다 (POSIX만)."""
    if os.name != "posix" or not path.exists():
        return None
    mode = path.stat().st_mode
    if mode & (stat.S_IROTH | stat.S_IWOTH):
        return f"{path} 를 모든 사용자가 읽을 수 있습니다. chmod o-rwx {path} 를 권장합니다."
    return None
