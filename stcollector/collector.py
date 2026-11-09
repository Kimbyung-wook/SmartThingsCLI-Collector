"""수집 루프: 기기 목록 갱신, 이벤트 이력(우선) 또는 상태 조회(대안) 수집."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from .auth import ReauthRequired
from .cli_client import SmartThingsCliClient
from .client import SmartThingsClient
from .config import Config, ConfigError
from .db import Database, Reading
from .transport import HttpError

log = logging.getLogger(__name__)

DAY_MS = 86_400_000
# 이력이 늦게 도착하는 경우를 대비해 매번 10분 겹쳐서 조회한다 (중복은 DB가 걸러낸다).
HISTORY_OVERLAP_MS = 10 * 60 * 1000


# ---- 값 변환 ---------------------------------------------------------------
def split_value(value: object) -> tuple[float | None, str | None] | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return (None, "true" if value else "false")
    if isinstance(value, (int, float)):
        return (float(value), None)
    if isinstance(value, str):
        return (None, value)
    return (None, json.dumps(value, ensure_ascii=False, sort_keys=True))


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def iso_to_ms(text: str) -> int:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - _EPOCH) // timedelta(milliseconds=1)  # 정수 연산: 부동소수점 오차 없음


def is_tracked(attributes: dict[str, list[str]], capability: str, attribute: str) -> bool:
    return attribute in attributes.get(capability, ())


def history_item_to_row(item: dict, attributes: dict[str, list[str]]) -> Reading | None:
    cap, attr = item.get("capability"), item.get("attribute")
    if not is_tracked(attributes, cap, attr):
        return None
    split = split_value(item.get("value"))
    if split is None:
        return None
    return (
        item["deviceId"],
        item.get("component") or "main",
        cap,
        attr,
        int(item["epoch"]),
        split[0],
        split[1],
        item.get("unit"),
        "history",
        item.get("hash"),
    )


def status_to_rows(device_id: str, status: dict, attributes: dict[str, list[str]]) -> list[Reading]:
    rows: list[Reading] = []
    for component, caps in (status.get("components") or {}).items():
        for cap, attrs in (caps or {}).items():
            for attr, state in (attrs or {}).items():
                if not is_tracked(attributes, cap, attr) or not isinstance(state, dict):
                    continue
                ts = state.get("timestamp")
                split = split_value(state.get("value"))
                if not ts or split is None:
                    continue
                rows.append(
                    (device_id, component, cap, attr, iso_to_ms(ts), split[0], split[1],
                     state.get("unit"), "status", None)
                )
    return rows


def device_capabilities(device: dict) -> set[str]:
    caps: set[str] = set()
    for comp in device.get("components") or []:
        for cap in comp.get("capabilities") or []:
            if cap.get("id"):
                caps.add(cap["id"])
    return caps


# ---- 수집기 ----------------------------------------------------------------
class Collector:
    def __init__(
        self, cfg: Config, client: SmartThingsClient | SmartThingsCliClient, db: Database, clock=time.time
    ):
        self.cfg = cfg
        self.client = client
        self.db = db
        self.clock = clock
        self.location_id = cfg.location_id
        self.devices: list[dict] = []
        self.mode: str | None = None if cfg.mode == "auto" else cfg.mode
        self._next_status: dict[str, float] = {}
        self._stop: threading.Event | None = None

    def _uses_cli(self) -> bool:
        return self.cfg.backend == "cli"

    def _now_ms(self) -> int:
        return int(self.clock() * 1000)

    # ---- 기기 -------------------------------------------------------------
    def resolve_location(self) -> str:
        if self.location_id:
            return self.location_id
        locations = self.client.locations()
        if len(locations) != 1:
            choices = ", ".join(f"{l.get('name')}={l.get('locationId')}" for l in locations)
            raise ConfigError(f"위치가 {len(locations)}개입니다. 설정의 location_id 를 지정하세요: {choices}")
        self.location_id = locations[0]["locationId"]
        self.db.set_meta("location_id", self.location_id)
        return self.location_id

    def refresh_devices(self) -> list[dict]:
        loc = self.resolve_location()
        rooms = {r.get("roomId"): r.get("name") for r in self.client.rooms(loc)}
        tracked_caps = set(self.cfg.attributes)
        devices = []
        for d in self.client.devices(loc):
            caps = device_capabilities(d) & tracked_caps
            if not caps:
                continue
            devices.append(
                {
                    "device_id": d["deviceId"],
                    "label": d.get("label") or d.get("name"),
                    "name": d.get("name"),
                    "room_id": d.get("roomId"),
                    "room_name": rooms.get(d.get("roomId")),
                    "location_id": loc,
                    "capabilities": caps,
                }
            )
        self.db.upsert_devices(devices)
        self.devices = devices
        log.info("수집 대상 기기 %d개", len(devices))
        return devices

    # ---- 방식 결정 ----------------------------------------------------------
    def detect_mode(self) -> str:
        if self.cfg.mode != "auto":
            self.mode = self.cfg.mode
        elif not self.devices:
            self.mode = "status"
        else:
            probe = self.devices[0]["device_id"]
            try:
                if self._uses_cli():
                    pages = self.client.location_history_pages(self.location_id, limit=1)
                else:
                    pages = self.client.device_history_pages(self.location_id, probe, limit=1)
                next(iter(pages), None)
                self.mode = "history"
            except HttpError as e:
                if e.status in (400, 403, 404):
                    log.warning("이벤트 이력 API 사용 불가 (HTTP %s) → 상태 조회 방식으로 수집합니다", e.status)
                    self.mode = "status"
                else:
                    raise
        self.db.set_meta("mode", self.mode)
        log.info("수집 방식: %s", self.mode)
        return self.mode

    # ---- 이벤트 이력 ---------------------------------------------------------
    def poll_history(self) -> int:
        now_ms = self._now_ms()
        if self._uses_cli():
            return self._poll_history_location(now_ms)
        total = 0
        for dev in self.devices:
            total += self._poll_history_device(dev["device_id"], now_ms)
        return total

    def _poll_history_location(self, now_ms: int) -> int:
        """위치 단위 이력을 과거→최신 방향으로 읽는다. 시작점은 위치 커서 하나로 정한다.

        기기별 커서로 정하면 이벤트가 드문 기기 때문에 조회 범위가 계속 넓어진다.
        페이지마다 커서를 옮기므로 실패하거나 페이지 한도에 닿아도 다음 주기에 그 지점부터 잇는다.
        """
        key = f"history_cursor:{self.location_id}"
        stored = self.db.get_meta(key)
        cursor = int(stored) if stored is not None else None
        first = cursor is None
        since = now_ms - self.cfg.backfill_days * DAY_MS if first else cursor - HISTORY_OVERLAP_MS
        known = {dev["device_id"] for dev in self.devices}
        if first:
            # 백필이 중간에 끊겨도 다음 주기에 이 기기들을 새 기기로 오인하지 않게 한다.
            for device_id in known:
                if self.db.get_cursor(device_id) is None:
                    self.db.set_cursor(device_id, since)
        newest = max(cursor or 0, since if first else 0)
        total = 0
        pages = 0
        max_pages = self.cfg.history_backfill_max_pages if first else self.cfg.history_max_pages
        for items in self.client.location_history_pages(
            self.location_id, limit=self.cfg.history_page_limit, after_ms=since
        ):
            pages += 1
            rows: list[Reading] = []
            newest_by_device: dict[str, int] = {}
            for item in items:
                epoch = int(item["epoch"])
                if epoch < since:
                    continue
                newest = max(newest, epoch)
                device_id = item.get("deviceId")
                if device_id not in known:
                    continue
                newest_by_device[device_id] = max(newest_by_device.get(device_id, 0), epoch)
                row = history_item_to_row(item, self.cfg.attributes)
                if row:
                    rows.append(row)
            total += self.db.insert_readings(rows)
            self.db.set_meta(key, newest)
            for device_id, epoch in newest_by_device.items():
                self.db.set_cursor(device_id, max(epoch, self.db.get_cursor(device_id) or 0))
            if self._stop is not None and self._stop.is_set():
                return total
            if pages >= max_pages:
                log.info("위치 이력 %d페이지 한도에 도달 — 나머지는 다음 주기에 이어서 받습니다", max_pages)
                break

        for device_id in known:
            if self.db.get_cursor(device_id) is None:
                # 새로 추가된 기기만 기기별로 백필한다 (기기당 CLI 몇 회).
                total += self._poll_history_device(device_id, now_ms)
        return total

    def _poll_history_device(self, device_id: str, now_ms: int) -> int:
        cursor = self.db.get_cursor(device_id)
        first = cursor is None
        since = now_ms - self.cfg.backfill_days * DAY_MS if first else cursor - HISTORY_OVERLAP_MS
        max_pages = self.cfg.history_backfill_max_pages if first else self.cfg.history_max_pages

        rows: list[Reading] = []
        newest = cursor or 0
        pages = 0
        reached = False
        for items in self.client.device_history_pages(
            self.location_id, device_id, limit=self.cfg.history_page_limit
        ):
            pages += 1
            for item in items:
                epoch = int(item["epoch"])
                if epoch < since:
                    reached = True
                    continue
                newest = max(newest, epoch)
                row = history_item_to_row(item, self.cfg.attributes)
                if row:
                    rows.append(row)
            if reached:
                break
            if pages >= max_pages:
                log.warning(
                    "%s: 이력 %d페이지 한도에 도달 — 그 이전 이벤트는 받지 못했을 수 있습니다",
                    device_id, max_pages,
                )
                break

        inserted = self.db.insert_readings(rows)
        self.db.set_cursor(device_id, max(newest, cursor or 0, since if first else 0))
        if inserted:
            log.debug("%s: 이력 %d건 추가", device_id, inserted)
        return inserted

    # ---- 상태 조회 ----------------------------------------------------------
    def status_interval(self, dev: dict) -> int:
        return min(self.cfg.status_intervals.get(c, 300) for c in dev["capabilities"])

    def _poll_status_bulk(self, now: float) -> int:
        """CLI 1회로 전 기기 상태를 읽는다. CLI 비용이 커서 history_interval_s 보다 자주 읽지 않는다."""
        if not self.devices or any(self._next_status.get(d["device_id"], 0) > now for d in self.devices):
            return 0
        device_ids = [d["device_id"] for d in self.devices]
        total = 0
        for device_id, status in self.client.device_statuses(self.location_id, device_ids).items():
            total += self.db.insert_readings(status_to_rows(device_id, status, self.cfg.attributes))
        interval = max(self.cfg.history_interval_s, min(self.status_interval(d) for d in self.devices))
        self._next_status = dict.fromkeys(device_ids, now + interval)
        return total

    def poll_status_due(self) -> int:
        now = self.clock()
        if self._uses_cli():
            return self._poll_status_bulk(now)
        total = 0
        for dev in self.devices:
            did = dev["device_id"]
            if self._next_status.get(did, 0) > now:
                continue
            status = self.client.device_status(did)
            total += self.db.insert_readings(status_to_rows(did, status, self.cfg.attributes))
            self._next_status[did] = now + self.status_interval(dev)
        return total

    def next_status_due(self) -> float:
        return min(self._next_status.values(), default=self.clock())

    # ---- 메인 루프 ----------------------------------------------------------
    def run(self, stop: threading.Event) -> None:
        self._stop = stop
        next_refresh = 0.0
        next_history = 0.0
        backoff = 0.0
        while not stop.is_set():
            now = self.clock()
            try:
                if now >= next_refresh:
                    self.refresh_devices()
                    if self.mode is None or self.cfg.mode == "auto":
                        self.detect_mode()
                    next_refresh = now + self.cfg.device_refresh_s

                inserted = 0
                if self.mode == "history" and now >= next_history:
                    inserted = self.poll_history()
                    next_history = now + self.cfg.history_interval_s
                elif self.mode == "status":
                    inserted = self.poll_status_due()

                self.db.set_meta("last_ok_ms", self._now_ms())
                self.db.set_meta("auth_state", "ok")
                if inserted:
                    log.info("새 측정값 %d건 저장", inserted)
                backoff = 0.0
            except ReauthRequired as e:
                log.error("재인증 필요: %s — `stcollector auth` 를 실행하세요", e)
                self.db.set_meta("auth_state", "reauth_required")
                self.db.set_meta("last_error", str(e))
                stop.wait(300)
                continue
            except ConfigError:
                raise
            except Exception as e:  # 네트워크/일시 오류: 기록하고 점점 늦춰서 재시도
                backoff = min(900.0, max(30.0, backoff * 2))
                log.exception("수집 오류, %.0f초 후 재시도", backoff)
                self.db.set_meta("last_error", f"{type(e).__name__}: {e}")
                self.db.set_meta("last_error_ms", self._now_ms())
                stop.wait(backoff)
                continue

            if self.mode == "history":
                wake = next_history
            else:
                wake = self.next_status_due()
            stop.wait(max(1.0, min(wake, next_refresh) - self.clock()))
