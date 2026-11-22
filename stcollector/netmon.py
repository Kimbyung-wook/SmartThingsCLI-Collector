"""집 공유기의 인터넷(WAN) 송수신량 모니터.

공유기의 UPnP WANCommonInterfaceConfig 서비스에서 누적 바이트(받기·보내기)를 interval_s 마다 읽어
직전 값과의 차이를 저장한다. 공유기 관리자 로그인과 SNMP 는 쓰지 않는다.

- 보내는 UPnP 액션은 ALLOWED_ACTIONS 의 읽기 액션뿐이다. 포트 매핑 추가·삭제 같은 쓰기 액션은
  call() 이 요청을 만들기 전에 거절한다 (cli_client.ALLOWED_COMMANDS 와 같은 방식).
- 통신 상대는 설정의 공유기 IP 하나뿐이다. SSDP 응답이나 장치 설명의 URL 이 다른 주소를 가리키면 쓰지 않는다.
- 카운터는 32비트일 수 있다 (4GB 에서 0으로). 이전 값보다 작으면 2^32 을 더하고, 그 결과가 링크 속도로
  불가능한 양이면(공유기 재부팅으로 0이 된 경우) 그 구간을 버린다. 읽기 실패나 긴 공백도 결측으로 둔다.
- 저장은 수집 DB 와 분리된 netmon.db 다. 1분 기록은 메모리에 모았다가 flush_s 마다 저장하고(microSD 쓰기 줄이기),
  끝난 시간은 1시간 요약(hours)으로 묶는다. 1분 기록은 보관 기간이 지나면 지우고 1시간 요약은 지우지 않는다.
- 현재값은 tmpfs(/run)의 JSON 에 매 주기 쓴다. 아직 저장하지 않은 1분 기록도 함께 실어 화면이 바로 보이게 한다.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .settings import NetmonSettings, netmon_db_path

log = logging.getLogger(__name__)

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
COUNTER_MOD = 2**32
RATE_SLACK = 1.1           # 링크 속도보다 이만큼 넘으면 불가능한 양으로 본다
LINK_REFRESH_MS = 600_000  # 링크 속도는 10분마다 다시 읽는다
REDISCOVER_MS = 600_000    # 찾기에 실패하면 10분 뒤에 다시 찾는다 (멀티캐스트를 줄인다)
MAX_BODY = 65_536

SSDP_ADDR = ("239.255.255.250", 1900)
IGD_TYPE = "urn:schemas-upnp-org:device:InternetGatewayDevice:1"
WAN_COMMON = "urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1"

# 보낼 수 있는 UPnP 액션 (모두 읽기). 이 밖의 액션은 보내지 않는다.
ALLOWED_ACTIONS = frozenset({"GetTotalBytesReceived", "GetTotalBytesSent", "GetCommonLinkProperties"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS minutes (
    ts_ms     INTEGER PRIMARY KEY,   -- 구간 끝 시각 (UTC ms). 집계는 이 시각이 든 시간에 넣는다
    span_ms   INTEGER NOT NULL,      -- 직전 읽기부터의 길이
    rx_bytes  INTEGER NOT NULL,      -- 받은 양 (인터넷 -> 집)
    tx_bytes  INTEGER NOT NULL       -- 보낸 양
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hours (
    ts_ms      INTEGER PRIMARY KEY,  -- 시 시작 시각 (UTC ms, KST 와 경계가 같다)
    rx_bytes   INTEGER NOT NULL,
    tx_bytes   INTEGER NOT NULL,
    covered_ms INTEGER NOT NULL,     -- 기록이 있는 길이 (3_600_000 이면 빠짐 없음)
    rx_max_bps REAL,                 -- 구간(약 1분) 평균 속도의 최댓값
    tx_max_bps REAL,
    samples    INTEGER NOT NULL
) WITHOUT ROWID;
"""


class UpnpError(Exception):
    pass


@dataclass
class Reading:
    ts_ms: int
    rx: int
    tx: int


# ---- UPnP (SSDP 찾기, 장치 설명, SOAP) --------------------------------------------------
def _parse_xml(body: bytes) -> ET.Element:
    """LAN 기기가 준 XML. 크기를 제한하고 DTD·엔티티는 받지 않는다 (엔티티 확장 공격 막기)."""
    if len(body) > MAX_BODY:
        raise UpnpError("응답이 너무 큽니다")
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise UpnpError("DTD 가 든 XML 은 받지 않습니다")
    try:
        return ET.fromstring(body)
    except ET.ParseError as e:
        raise UpnpError(f"XML 해석 실패: {e}") from None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_ssdp_location(data: bytes) -> str | None:
    """SSDP 응답의 LOCATION 헤더."""
    for line in data.decode("latin-1", "replace").split("\r\n")[1:]:
        key, _, value = line.partition(":")
        if key.strip().lower() == "location":
            return value.strip() or None
    return None


def parse_control_url(body: bytes, location: str) -> str | None:
    """장치 설명(rootDesc.xml)에서 WANCommonInterfaceConfig 의 controlURL (절대 URL)."""
    root = _parse_xml(body)
    base = location
    for el in root.iter():
        if _local(el.tag) == "URLBase" and el.text and el.text.strip():
            base = el.text.strip()
    for service in root.iter():
        if _local(service.tag) != "service":
            continue
        fields = {_local(c.tag): (c.text or "").strip() for c in service}
        if fields.get("serviceType") == WAN_COMMON and fields.get("controlURL"):
            return urllib.parse.urljoin(base, fields["controlURL"])
    return None


def soap_request(action: str) -> bytes:
    if action not in ALLOWED_ACTIONS:
        raise PermissionError(f"허용하지 않은 UPnP 액션입니다: {action}")
    return (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{WAN_COMMON}"/></s:Body></s:Envelope>'
    ).encode("ascii")


def parse_soap_response(body: bytes, action: str) -> dict[str, str]:
    root = _parse_xml(body)
    for el in root.iter():
        if _local(el.tag) == action + "Response":
            return {_local(c.tag): (c.text or "").strip() for c in el}
    for el in root.iter():
        if _local(el.tag) == "Fault":
            raise UpnpError(f"{action} 실패 (SOAP Fault)")
    raise UpnpError(f"{action} 응답 형식이 다릅니다")


def ssdp_discover(router_ip: str, timeout: float = 2.0) -> str | None:
    """M-SEARCH 로 인터넷 공유기를 찾는다. 설정한 공유기 IP 가 보낸 응답만 쓴다."""
    msg = ("M-SEARCH * HTTP/1.1\r\n"
           f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
           'MAN: "ssdp:discover"\r\n'
           "MX: 1\r\n"
           f"ST: {IGD_TYPE}\r\n\r\n").encode("ascii")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(timeout)
        sock.sendto(msg, SSDP_ADDR)
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            sock.settimeout(left)
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                return None
            if addr[0] != router_ip:
                continue
            location = parse_ssdp_location(data)
            if location and _host_of(location) == router_ip:
                return location
    finally:
        sock.close()


def _host_of(url: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    return parsed.hostname if parsed.scheme == "http" else None


class _Http:
    """공유기에만 보내는 작은 HTTP 클라이언트. 프록시를 쓰지 않고 리다이렉트를 따르지 않는다."""

    def __init__(self, router_ip: str, timeout: float):
        self.router_ip, self.timeout = router_ip, timeout

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)

    def __call__(self, url: str, data: bytes | None = None, headers: dict | None = None) -> bytes:
        if _host_of(url) != self.router_ip:
            raise UpnpError("공유기가 아닌 주소로는 보내지 않습니다")
        req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data else "GET")
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                return resp.read(MAX_BODY + 1)
        except (urllib.error.URLError, OSError) as e:
            raise UpnpError(f"공유기 연결 실패: {e}") from None


class WanReader:
    """공유기의 누적 송수신 바이트를 읽는다. 실패하면 UpnpError."""

    def __init__(self, router_ip: str, location: str | None = None, timeout: float = 3.0,
                 http=None, discover=ssdp_discover, clock=time.time):
        ipaddress.ip_address(router_ip)  # 주소 형식 확인 (설정 단계에서도 본다)
        self.router_ip, self.location = router_ip, location or None
        self.http = http or _Http(router_ip, timeout)
        self.discover, self.clock = discover, clock
        self.control_url: str | None = None
        self.link: dict = {}
        self._link_at = 0
        self._next_discover = 0

    def _find_control_url(self) -> str:
        now = int(self.clock() * 1000)
        candidates = []
        if now >= self._next_discover:
            self._next_discover = now + REDISCOVER_MS
            try:
                found = self.discover(self.router_ip)
            except OSError as e:
                log.debug("SSDP 찾기 실패: %s", e)
                found = None
            if found:
                candidates.append(found)
        if self.location and self.location not in candidates:
            candidates.append(self.location)  # 포트가 바뀌지 않았다면 설정값으로도 된다
        for location in candidates:
            if _host_of(location) != self.router_ip:
                continue
            try:
                url = parse_control_url(self.http(location), location)
            except UpnpError as e:
                log.debug("장치 설명 읽기 실패 (%s): %s", location, e)
                continue
            if url and _host_of(url) == self.router_ip:
                return url
        raise UpnpError("공유기의 WANCommonInterfaceConfig 서비스를 찾지 못했습니다")

    def call(self, action: str) -> dict[str, str]:
        body = soap_request(action)  # 허용 목록 밖이면 여기서 PermissionError
        if self.control_url is None:
            self.control_url = self._find_control_url()
        headers = {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{WAN_COMMON}#{action}"'}
        try:
            return parse_soap_response(self.http(self.control_url, body, headers), action)
        except UpnpError:
            self.control_url = None  # 다음에 다시 찾는다 (공유기 재부팅으로 포트가 바뀌었을 수 있다)
            raise

    def _counter(self, action: str, field: str) -> int:
        raw = self.call(action).get(field, "")
        if not raw.isdigit():
            raise UpnpError(f"{field} 값이 숫자가 아닙니다")
        return int(raw)

    def read(self) -> Reading:
        rx = self._counter("GetTotalBytesReceived", "NewTotalBytesReceived")
        tx = self._counter("GetTotalBytesSent", "NewTotalBytesSent")
        ts = int(self.clock() * 1000)
        if ts - self._link_at >= LINK_REFRESH_MS:
            self._link_at = ts
            try:
                props = self.call("GetCommonLinkProperties")
                self.link = {
                    "down_bps": _int_or_none(props.get("NewLayer1DownstreamMaxBitRate")),
                    "up_bps": _int_or_none(props.get("NewLayer1UpstreamMaxBitRate")),
                    "status": props.get("NewPhysicalLinkStatus") or None,
                }
            except UpnpError as e:
                log.debug("링크 정보 읽기 실패: %s", e)
        return Reading(ts, rx, tx)


def _int_or_none(raw) -> int | None:
    return int(raw) if isinstance(raw, str) and raw.isdigit() and int(raw) > 0 else None


# ---- 증분 계산 ----------------------------------------------------------------------
def counter_delta(prev: int, cur: int, span_ms: int, max_bps: float) -> int | None:
    """누적값 차이 (바이트). 랩어라운드는 2^32 을 더하고, 링크 속도로 불가능한 양이면 None (재부팅으로 0이 됨)."""
    delta = cur - prev if cur >= prev else cur + COUNTER_MOD - prev
    if span_ms <= 0 or delta * 8 * 1000 / span_ms > max_bps * RATE_SLACK:
        return None
    return delta


class NetStore:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def add_minutes(self, rows: list[tuple]) -> None:
        if rows:
            with self.conn:
                self.conn.executemany(
                    "INSERT OR REPLACE INTO minutes(ts_ms, span_ms, rx_bytes, tx_bytes) VALUES(?,?,?,?)", rows)

    def rollup_hours(self, until_ms: int) -> int:
        """until_ms 앞의 끝난 시간을 1분 기록으로 묶는다. meta hours_done 다음 시부터 이어 간다."""
        until = until_ms - until_ms % HOUR_MS
        done = self.get_meta("hours_done")
        if done is not None and done.isdigit():
            start = int(done)
        else:
            row = self.conn.execute("SELECT MIN(ts_ms) FROM minutes").fetchone()
            if row[0] is None:
                return 0
            start = row[0] - row[0] % HOUR_MS
        if start >= until:
            return 0
        out = hour_rows(self.conn.execute(
            "SELECT ts_ms, span_ms, rx_bytes, tx_bytes FROM minutes WHERE ts_ms>=? AND ts_ms<? ORDER BY ts_ms",
            (start, until)).fetchall())
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO hours(ts_ms, rx_bytes, tx_bytes, covered_ms, rx_max_bps, "
                                  "tx_max_bps, samples) VALUES(?,?,?,?,?,?,?)", out)
            self.conn.execute("INSERT INTO meta(key, value) VALUES('hours_done', ?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(until),))
        return len(out)

    def apply_retention(self, now_ms: int, minute_days: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM minutes WHERE ts_ms<?", (now_ms - minute_days * DAY_MS,))


def hour_rows(minutes) -> list[tuple]:
    """1분 기록 (ts_ms, span_ms, rx, tx) 들을 시간별 (시각, rx, tx, covered_ms, rx_max_bps, tx_max_bps, 개수) 로."""
    groups: dict[int, list] = {}
    for ts, span, rx, tx in minutes:
        g = groups.setdefault(ts - ts % HOUR_MS, [0, 0, 0, None, None, 0])
        g[0] += rx
        g[1] += tx
        g[2] += span
        rx_bps, tx_bps = rx * 8000 / span, tx * 8000 / span
        g[3] = rx_bps if g[3] is None else max(g[3], rx_bps)
        g[4] = tx_bps if g[4] is None else max(g[4], tx_bps)
        g[5] += 1
    return [(h, g[0], g[1], min(g[2], HOUR_MS), round(g[3], 1), round(g[4], 1), g[5]) for h, g in sorted(groups.items())]


class NetMonitor:
    """읽은 누적값으로 1분 기록을 만든다. 입출력은 reader, store 쪽에 있다."""

    def __init__(self, store: NetStore, settings: NetmonSettings):
        self.store, self.settings = store, settings
        self.prev: Reading | None = None
        self.pending: list[tuple] = []
        self.last_flush_ms: int | None = None
        self.last_retention_ms: int | None = None
        self.last: dict | None = None    # 마지막으로 저장한 구간의 속도
        self.error: str | None = None
        self.error_since: int | None = None
        self.dropped = 0                 # 버린 구간 수 (재부팅·긴 공백)

    @property
    def max_span_ms(self) -> int:
        """이보다 긴 공백 뒤의 차이는 쓰지 않는다 (여러 번 돌았을 수 있다)."""
        return 3 * self.settings.interval_s * 1000

    def max_bps(self, link: dict) -> float:
        rates = [v for v in (link.get("down_bps"), link.get("up_bps")) if v]
        return float(max(rates)) if rates else self.settings.max_mbps * 1e6

    def step(self, now_ms: int, reading: Reading | None, link: dict | None = None, error: str | None = None) -> None:
        if self.last_flush_ms is None:
            self.last_flush_ms = now_ms
        if reading is None:
            if self.error is None:
                self.error_since = now_ms
            self.error = error or "읽기 실패"
        else:
            self.error = self.error_since = None
            prev, self.prev = self.prev, reading
            if prev is not None:
                span = reading.ts_ms - prev.ts_ms
                limit = self.max_bps(link or {})
                rx = counter_delta(prev.rx, reading.rx, span, limit)
                tx = counter_delta(prev.tx, reading.tx, span, limit)
                if 0 < span <= self.max_span_ms and rx is not None and tx is not None:
                    self.pending.append((reading.ts_ms, span, rx, tx))
                    self.last = {"ts_ms": reading.ts_ms, "rx_bps": round(rx * 8000 / span, 1),
                                 "tx_bps": round(tx * 8000 / span, 1)}
                else:
                    self.dropped += 1
                    log.info("구간을 버립니다 (길이 %d초, 공유기 재부팅이나 긴 공백)", span // 1000)
        if now_ms - self.last_flush_ms >= self.settings.flush_s * 1000:
            self.flush(now_ms)
        if self.last_retention_ms is None or now_ms - self.last_retention_ms >= DAY_MS:
            self.store.apply_retention(now_ms, self.settings.minute_retention_days)
            self.last_retention_ms = now_ms

    def flush(self, now_ms: int) -> None:
        self.store.add_minutes(self.pending)
        self.pending = []
        self.store.rollup_hours(now_ms)  # 지금까지 읽은 것은 모두 저장됐다
        self.last_flush_ms = now_ms

    def snapshot(self, now_ms: int, link: dict) -> dict:
        return {"ts_ms": now_ms, "last": self.last, "link": link, "error": self.error, "error_since": self.error_since,
                "dropped": self.dropped, "interval_s": self.settings.interval_s,
                "pending": [list(r) for r in self.pending]}


def write_runtime(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")
    tmp.replace(path)


def run_netmon(db_path: Path, settings: NetmonSettings, stop: threading.Event,
               reader: WanReader | None = None, clock=time.time) -> int:
    if not settings.enabled:
        log.info("인터넷 사용량 모니터가 꺼져 있습니다 ([netmon] enabled = false)")
        return 0
    reader = reader or WanReader(settings.router, settings.location, clock=clock)
    store = NetStore(netmon_db_path(db_path))
    monitor = NetMonitor(store, settings)
    runtime = Path(settings.runtime_file)
    runtime_ok, failing = True, False
    store.rollup_hours(int(clock() * 1000))
    log.info("인터넷 사용량 모니터 시작 (공유기 %s, 주기 %ds)", settings.router, settings.interval_s)
    next_at = time.monotonic()
    try:
        while not stop.is_set():
            now = int(clock() * 1000)
            try:
                reading, error = reader.read(), None
                if failing:
                    log.info("공유기 읽기가 다시 됩니다")
                failing = False
            except (UpnpError, OSError) as e:
                reading, error = None, str(e)
                if not failing:
                    log.warning("공유기 송수신량을 읽지 못했습니다 (결측으로 둡니다): %s", e)
                failing = True
            monitor.step(now, reading, reader.link, error)
            try:
                write_runtime(runtime, monitor.snapshot(now, reader.link))
                runtime_ok = True
            except OSError as e:
                if runtime_ok:
                    log.warning("현재값 파일을 쓰지 못했습니다: %s", e)
                runtime_ok = False
            next_at += settings.interval_s
            stop.wait(max(0.0, next_at - time.monotonic()))
    finally:
        monitor.flush(int(clock() * 1000))
        store.close()
    log.info("인터넷 사용량 모니터 종료")
    return 0
