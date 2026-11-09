"""CLI: python -m stcollector --config <path> {auth,check,run,intervals,rollup,web,sysmon,hash-password}"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import signal
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import __version__
from .auth import Authenticator, ReauthRequired, TokenStore
from .cli_client import SmartThingsCliClient
from .client import SmartThingsClient
from .collector import Collector, device_capabilities, status_to_rows
from .config import ConfigError, load_config, permission_warning
from .db import Database
from .intervals import format_report, interval_report
from .transport import HttpError, TransportError

log = logging.getLogger("stcollector")
DEFAULT_CONFIG = os.environ.get("STCOLLECTOR_CONFIG", "/etc/stcollector/config.toml")


def _build(cfg):
    """(인증 객체, 클라이언트)를 만든다. CLI 방식에는 인증 객체가 없다."""
    if cfg.backend == "cli":
        return None, SmartThingsCliClient(cfg)
    cfg.require_credentials()
    auth = Authenticator(cfg, TokenStore(cfg.token_path))
    return auth, SmartThingsClient(cfg, auth)


@contextmanager
def _single_instance(db_path: Path):
    """같은 DB에 수집기가 두 개 붙지 않도록 잠근다 (POSIX)."""
    if os.name != "posix":
        yield
        return
    import fcntl

    lock_path = db_path.with_suffix(db_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("다른 수집기가 이미 실행 중입니다 (systemctl status stcollector 확인)")
        yield


# ---- 명령 ------------------------------------------------------------------
def cmd_auth(cfg, args) -> int:
    if cfg.backend == "cli":
        client = SmartThingsCliClient(cfg)
        print("backend = \"cli\" 에서는 OAuth 승인을 쓰지 않습니다.")
        print("README의 SmartThings CLI 로그인 절차를 따라 아래 위치에 로그인 정보를 만드세요.\n")
        print(f"  CLI 실행 파일: {cfg.cli_path}")
        print(f"  CLI HOME:      {cfg.cli_home}")
        print(f"  로그인 정보:   {client.credentials_path.as_posix()}")
        return 0
    auth, _ = _build(cfg)
    state = secrets.token_urlsafe(16)
    print("1) 아래 URL을 PC나 스마트폰 브라우저에서 열고 SmartThings 계정으로 로그인·승인하세요.\n")
    print("   " + auth.authorize_url(state) + "\n")
    print(f"2) 승인 후 브라우저가 {cfg.redirect_uri}?code=... 로 이동합니다.")
    print("   페이지가 열리지 않아도 정상입니다. 주소창의 전체 URL을 복사해 아래에 붙여 넣으세요.\n")
    pasted = input("리다이렉트 URL: ")
    try:
        code = Authenticator.parse_redirect(pasted, state)
        auth.exchange_code(code)
    except (ValueError, ReauthRequired) as e:
        print(f"\n실패: {e}", file=sys.stderr)
        return 1
    st = auth.status()
    print(f"\n완료. 토큰 저장: {cfg.token_path} (만료까지 {st['expires_in_h']}시간, 이후 자동 갱신)")
    print(f"승인된 권한: {st.get('scope')}")
    return 0


def cmd_check(cfg, args) -> int:
    """연결과 API 동작을 점검한다. DB에는 쓰지 않는다."""
    auth, client = _build(cfg)
    uses_cli = cfg.backend == "cli"
    if uses_cli:
        print(f"[CLI] {cfg.cli_path} (로그인 정보 {'있음' if client.credentials_path.exists() else '없음'})")
    else:
        print(f"[토큰] {auth.status()}")

    locations = client.locations()
    print(f"[위치] {[(l.get('name'), l.get('locationId')) for l in locations]}")
    loc = cfg.location_id or (locations[0]["locationId"] if len(locations) == 1 else None)
    if not loc:
        print("위치가 여러 개입니다. 설정에 location_id 를 지정하세요.")
        return 1

    rooms = {r.get("roomId"): r.get("name") for r in client.rooms(loc)}
    tracked = set(cfg.attributes)
    devices = [d for d in client.devices(loc) if device_capabilities(d) & tracked]
    print(f"\n[수집 대상 기기 {len(devices)}개]")
    for d in devices:
        caps = ", ".join(sorted(device_capabilities(d) & tracked))
        print(f"  - {d.get('label') or d.get('name')} / {rooms.get(d.get('roomId'), '-')} : {caps}")
    if not devices:
        return 1

    probe = devices[0]
    print(f"\n[상태 조회] {probe.get('label')}")
    for row in status_to_rows(probe["deviceId"], client.device_status(probe["deviceId"]), cfg.attributes):
        _, comp, cap, attr, ts, num, text, unit, *_ = row
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts / 1000))
        print(f"  {comp}.{cap}.{attr} = {num if num is not None else text} {unit or ''}  (보고 {when})")

    print(f"\n[이벤트 이력] {'위치 전체' if uses_cli else probe.get('label')} (최근 10건)")
    try:
        if uses_cli:
            pages = client.location_history_pages(loc, limit=10)
        else:
            pages = client.device_history_pages(loc, probe["deviceId"], limit=10)
        page = next(iter(pages), [])
    except HttpError as e:
        print(f"  사용 불가: HTTP {e.status} {e.body[:200]}")
        print("  → mode = \"auto\" 이면 상태 조회 방식으로 수집합니다.")
        return 0
    epochs = [int(i["epoch"]) for i in page]
    for i in page:
        when = time.strftime("%m-%d %H:%M:%S", time.localtime(int(i["epoch"]) / 1000))
        print(f"  {when}  {i.get('capability')}.{i.get('attribute')} = {i.get('value')} {i.get('unit') or ''}")
    order = "최신순" if epochs == sorted(epochs, reverse=True) else "최신순 아님 (확인 필요)"
    print(f"  정렬: {order}")
    return 0


def cmd_run(cfg, args) -> int:
    auth, client = _build(cfg)
    secret_path = client.credentials_path if cfg.backend == "cli" else cfg.token_path
    for p in (cfg.source_path, secret_path):
        if p and (w := permission_warning(p)):
            log.warning(w)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    with _single_instance(cfg.db_path):
        db = Database(cfg.db_path)
        try:
            log.info("수집 시작 (버전: %s, DB: %s, 방식 설정: %s)", __version__, cfg.db_path, cfg.mode)
            Collector(cfg, client, db).run(stop)
        finally:
            db.close()
    log.info("수집 종료")
    return 0


def cmd_intervals(cfg, args) -> int:
    db = Database(cfg.db_path)
    try:
        report = interval_report(db, args.days, int(time.time() * 1000))
        print(format_report(report, args.days))
    finally:
        db.close()
    return 0


def cmd_rollup(cfg, args) -> int:
    from .rollup import run_rollup
    from .settings import load_rollup_settings

    settings = load_rollup_settings(cfg.source_path)
    with _single_instance(Path(str(cfg.db_path) + ".rollup")):
        db = Database(cfg.db_path)
        try:
            started = time.monotonic()
            stats = run_rollup(db, settings, full=args.full)
        finally:
            db.close()
    log.info(
        "집계 완료: %.1f초, 항목 %d, 시간 행 %d, 일 %d, 주 %d, 원본 삭제 %d건%s",
        time.monotonic() - started, stats.series, stats.hours_written, stats.days_touched,
        stats.weeks_touched, stats.deleted_readings, " (전체 재계산)" if stats.full else "",
    )
    return 0


def cmd_web(cfg, args) -> int:
    from .settings import load_sysmon_settings, load_web_settings
    from .web.server import serve

    return serve(cfg.db_path, load_web_settings(cfg.source_path),
                 load_sysmon_settings(cfg.source_path).runtime_file)


def cmd_sysmon(cfg, args) -> int:
    from .settings import load_sysmon_settings, sysmon_db_path
    from .sysmon import run_monitor

    settings = load_sysmon_settings(cfg.source_path)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with _single_instance(sysmon_db_path(cfg.db_path)):
        return run_monitor(cfg.db_path, settings, stop)


def cmd_hash_password(cfg, args) -> int:
    import getpass

    from .web.session import hash_password

    first = getpass.getpass("대시보드 비밀번호 (8자 이상): ")
    if len(first) < 8:
        print("8자 이상이어야 합니다.", file=sys.stderr)
        return 1
    if getpass.getpass("한 번 더: ") != first:
        print("두 입력이 다릅니다.", file=sys.stderr)
        return 1
    print("\n설정 파일 [web] 섹션의 password_hash 줄을 아래 줄로 바꾸세요.")
    print("비밀번호 자체는 어디에도 저장되지 않습니다.\n")
    print(f'password_hash = "{hash_password(first)}"')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stcollector", description="SmartThings 센서 로컬 수집기")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"설정 파일 (기본: {DEFAULT_CONFIG})")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("auth", help="최초 OAuth 승인 (토큰 발급)")
    sub.add_parser("check", help="API 연결·기기·이력 API 점검 (읽기만)")
    sub.add_parser("run", help="수집 실행 (systemd 서비스용)")
    p_int = sub.add_parser("intervals", help="센서별 실제 보고 간격 리포트")
    p_int.add_argument("--days", type=int, default=7)
    p_rollup = sub.add_parser("rollup", help="시간·일·주 집계 (systemd timer용)")
    p_rollup.add_argument("--full", action="store_true", help="원본 전체에서 다시 계산")
    sub.add_parser("web", help="대시보드 웹서버 실행 (systemd 서비스용)")
    sub.add_parser("sysmon", help="RPi 상태 모니터 실행 (systemd 서비스용)")
    sub.add_parser("hash-password", help="대시보드 비밀번호 해시 만들기")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    handler = {
        "auth": cmd_auth, "check": cmd_check, "run": cmd_run, "intervals": cmd_intervals,
        "rollup": cmd_rollup, "web": cmd_web, "sysmon": cmd_sysmon, "hash-password": cmd_hash_password,
    }[args.command]
    try:
        return handler(cfg, args)
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2
    except ReauthRequired as e:
        print(f"인증 필요: {e}", file=sys.stderr)
        return 3
    except (HttpError, TransportError) as e:
        print(f"API 오류: {e}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
