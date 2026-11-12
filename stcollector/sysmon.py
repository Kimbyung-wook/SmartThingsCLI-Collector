"""Raspberry Pi 상태 모니터.

sysfs 와 /proc 만 읽는다. 외부 프로세스를 띄우지 않고 네트워크도 쓰지 않는다.
저장은 수집 DB 와 분리된 작은 SQLite(sysmon.db)에 하므로 수집·집계와 쓰기 경합이 없다.

- 현재값은 tmpfs(/run)의 JSON 에 매 주기 쓴다. microSD 에는 쓰지 않는다.
- 1분 요약은 메모리에 모았다가 flush_s 마다 한 번에 저장한다.
- 전압 부족·스로틀링·과열이 시작되면 직전 약 60초의 프로세스별 CPU 사용량과 함께 바로 저장한다.
  프로세스는 이름(comm)만 쓴다. 명령행은 비밀값이 있을 수 있어 읽지 않는다.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import sqlite3
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import ConfigError
from .settings import SysmonSettings, sysmon_db_path

log = logging.getLogger(__name__)

MINUTE_MS = 60_000
DAY_MS = 86_400_000
PROC_WINDOW_S = 60
TOP_PROCESSES = 8
CLEAR_AFTER_MS = 30_000  # 이 시간 동안 조건이 사라져야 해제로 본다 (깜빡임을 한 건으로 묶음)
KTHREAD_REFRESH_S = 60
RETENTION_CHECK_MS = DAY_MS
PF_KTHREAD = 0x00200000
_THROTTLED_DEFAULT = "devices/platform/soc/soc:firmware/get_throttled"
_UNIT_RE = re.compile(r"(stcollector(?:-[a-z]+)?)\.service")
_UNIT_LABELS = {
    "stcollector": "수집", "stcollector-web": "웹",
    "stcollector-rollup": "집계", "stcollector-sysmon": "모니터",
}
_UNSAFE_NAME = re.compile(r"[^\w .:/+\-]")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS minutes (
    ts_ms       INTEGER PRIMARY KEY,   -- 분 시작 시각 (UTC ms)
    temp_avg    REAL,
    temp_min    REAL,
    temp_max    REAL,
    freq_avg    REAL,                  -- MHz
    load_avg    REAL,
    cpu_avg     REAL,                  -- CPU 사용률 % (/proc/stat 표본 차이)
    mem_avail_mb INTEGER,              -- 그 분의 최솟값
    disk_free_gb REAL,
    undervolt_s INTEGER NOT NULL DEFAULT 0,
    overheat_s  INTEGER NOT NULL DEFAULT 0,
    throttle_s  INTEGER NOT NULL DEFAULT 0,
    samples     INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,      -- undervoltage, throttled, freq_capped, soft_temp, overheat, boot
    start_ms    INTEGER NOT NULL,
    end_ms      INTEGER,               -- 진행 중이면 NULL
    seconds     INTEGER NOT NULL DEFAULT 0,
    interrupted INTEGER NOT NULL DEFAULT 0,  -- 프로세스가 끝나 해제 시각을 모름
    temp_c      REAL,
    freq_mhz    REAL,
    load1       REAL,
    procs_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events (start_ms);
"""


@dataclass
class Sample:
    ts_ms: int
    temp_c: float | None = None
    freq_mhz: float | None = None
    load1: float | None = None
    cpu_pct: float | None = None       # 직전 표본 이후 CPU 사용률 %, 첫 표본은 None
    mem_total_mb: int | None = None
    mem_avail_mb: int | None = None
    uptime_s: int | None = None
    undervoltage: bool | None = None
    freq_capped: bool | None = None
    throttled: bool | None = None
    soft_temp: bool | None = None


def _read(path: Path) -> str | None:
    try:
        return path.read_text("ascii", "replace").strip()
    except OSError:
        return None


def parse_cpu_line(text: str) -> tuple[int, int] | None:
    """/proc/stat 첫 줄(cpu 합계)에서 (전체 tick, 쉰 tick). 쉰 시간은 idle + iowait.

    user~steal 8개만 더한다. guest 는 user 에 이미 들어 있다.
    """
    line = text.split("\n", 1)[0].split()
    if not line or line[0] != "cpu":
        return None
    try:
        values = [int(v) for v in line[1:9]]
    except ValueError:
        return None
    if len(values) < 5:
        return None
    return sum(values), values[3] + values[4]


def cpu_percent(prev: tuple[int, int] | None, cur: tuple[int, int] | None) -> float | None:
    if prev is None or cur is None:
        return None
    total, idle = cur[0] - prev[0], cur[1] - prev[1]
    if total <= 0 or idle < 0:
        return None
    return round(max(0.0, min(100.0, (total - idle) / total * 100)), 1)


def parse_stat(text: str) -> tuple[str, int, int, int] | None:
    """/proc/<pid>/stat 에서 (이름, flags, utime+stime, starttime)."""
    left, right = text.find("("), text.rfind(")")
    if left < 0 or right < 0:
        return None
    fields = text[right + 2:].split()
    try:
        return text[left + 1:right], int(fields[6]), int(fields[11]) + int(fields[12]), int(fields[19])
    except (IndexError, ValueError):
        return None


class SystemReader:
    def __init__(self, proc: Path = Path("/proc"), sys: Path = Path("/sys"),
                 throttled_path: Path | None = None, disk_path: Path = Path("/")):
        self.proc, self.sys, self.disk_path = Path(proc), Path(sys), Path(disk_path)
        self.throttled_path = throttled_path if throttled_path is not None else self.sys / _THROTTLED_DEFAULT
        self.has_throttled_file = self.throttled_path.exists()
        self._temp_file = self._find_hwmon("cpu_thermal", "temp1_input") or self.sys / "class/thermal/thermal_zone0/temp"
        self._uv_file = self._find_hwmon("rpi_volt", "in0_lcrit_alarm")
        self._freq_file = self.sys / "devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"
        self._kthreads: set[str] = set()
        self._kthreads_at = 0.0
        self._labels: dict[tuple[str, int], str] = {}
        self._cpu_prev: tuple[int, int] | None = None

    def _find_hwmon(self, name: str, attr: str) -> Path | None:
        root = self.sys / "class/hwmon"
        try:
            for entry in sorted(root.iterdir()):
                if _read(entry / "name") == name and (entry / attr).exists():
                    return entry / attr
        except OSError:
            pass
        return None

    @property
    def capabilities(self) -> dict:
        return {"undervoltage": self.has_throttled_file or self._uv_file is not None,
                "throttle_bits": self.has_throttled_file}

    def sample(self, ts_ms: int) -> Sample:
        s = Sample(ts_ms=ts_ms)
        raw = _read(self._temp_file)
        if raw and raw.lstrip("-").isdigit():
            s.temp_c = int(raw) / 1000
        raw = _read(self._freq_file)
        if raw and raw.isdigit():
            s.freq_mhz = int(raw) / 1000
        raw = _read(self.proc / "loadavg")
        if raw:
            try:
                s.load1 = float(raw.split()[0])
            except (ValueError, IndexError):
                pass
        raw = _read(self.proc / "stat")
        cur = parse_cpu_line(raw) if raw else None
        s.cpu_pct = cpu_percent(self._cpu_prev, cur)
        if cur is not None:
            self._cpu_prev = cur
        raw = _read(self.proc / "uptime")
        if raw:
            try:
                s.uptime_s = int(float(raw.split()[0]))
            except (ValueError, IndexError):
                pass
        raw = _read(self.proc / "meminfo")
        if raw:
            for line in raw.splitlines():
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    try:
                        kb = int(rest.split()[0])
                    except (ValueError, IndexError):
                        continue
                    if key == "MemTotal":
                        s.mem_total_mb = kb // 1024
                    else:
                        s.mem_avail_mb = kb // 1024
        self._read_power(s)
        return s

    def _read_power(self, s: Sample) -> None:
        if self.has_throttled_file:
            raw = _read(self.throttled_path)
            try:
                bits = int(raw, 16) if raw else None
            except ValueError:
                bits = None
            if bits is not None:
                s.undervoltage = bool(bits & 0x1)
                s.freq_capped = bool(bits & 0x2)
                s.throttled = bool(bits & 0x4)
                s.soft_temp = bool(bits & 0x8)
                return
        if self._uv_file is not None:
            raw = _read(self._uv_file)
            if raw in ("0", "1"):
                s.undervoltage = raw == "1"

    def disk(self) -> tuple[float, float] | None:
        try:
            usage = shutil.disk_usage(self.disk_path)
        except OSError:
            return None
        return usage.free / 1024**3, usage.total / 1024**3

    def boot_id(self) -> str | None:
        return _read(self.proc / "sys/kernel/random/boot_id")

    def _label(self, pid: str, comm: str, start: int) -> str:
        key = (pid, start)
        label = self._labels.get(key)
        if label is not None:
            return label
        safe = _UNSAFE_NAME.sub("?", comm)[:32] or "?"
        label = safe
        if safe.startswith("python"):  # 서비스가 모두 python3 이라 systemd 유닛으로 구분한다
            cgroup = _read(self.proc / pid / "cgroup") or ""
            match = _UNIT_RE.search(cgroup)
            if match:
                label = f"{safe} ({_UNIT_LABELS.get(match.group(1), match.group(1))})"
        self._labels[key] = label
        return label

    def processes(self, now_monotonic: float) -> dict[tuple[str, int], tuple[str, int]]:
        """{(pid, 시작 시각): (이름, 누적 CPU tick)}. 커널 스레드는 뺀다."""
        if now_monotonic - self._kthreads_at > KTHREAD_REFRESH_S:
            self._kthreads = set()
            self._kthreads_at = now_monotonic
        out = {}
        try:
            names = [e.name for e in os.scandir(self.proc) if e.name.isdigit()]
        except OSError:
            return out
        for pid in names:
            if pid in self._kthreads:
                continue
            try:
                with open(self.proc / pid / "stat", "rb") as f:
                    parsed = parse_stat(f.read().decode("utf-8", "replace"))
            except OSError:
                continue  # 읽는 사이 끝난 프로세스
            if parsed is None:
                continue
            comm, flags, ticks, start = parsed
            if flags & PF_KTHREAD:
                self._kthreads.add(pid)
                continue
            out[(pid, start)] = (self._label(pid, comm, start), ticks)
        self._labels = {k: v for k, v in self._labels.items() if k in out}
        return out


class SysmonStore:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """이전 판에서 만든 DB 에 새 열을 더한다."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(minutes)")}
        if "cpu_avg" not in cols:
            with self.conn:
                self.conn.execute("ALTER TABLE minutes ADD COLUMN cpu_avg REAL")

    def close(self) -> None:
        self.conn.close()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def add_minutes(self, rows: list[dict]) -> None:
        if not rows:
            return
        rows = [{"cpu_avg": None, **r} for r in rows]  # cpu_avg 가 없던 판의 현재값 파일에서 온 행
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO minutes(ts_ms, temp_avg, temp_min, temp_max, freq_avg, load_avg, cpu_avg, "
                "mem_avail_mb, disk_free_gb, undervolt_s, overheat_s, throttle_s, samples) VALUES(:ts_ms, :temp_avg, "
                ":temp_min, :temp_max, :freq_avg, :load_avg, :cpu_avg, :mem_avail_mb, :disk_free_gb, :undervolt_s, "
                ":overheat_s, :throttle_s, :samples)",
                rows,
            )

    def start_event(self, kind: str, start_ms: int, sample: Sample | None, procs: list[dict]) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO events(kind, start_ms, temp_c, freq_mhz, load1, procs_json) VALUES(?,?,?,?,?,?)",
                (kind, start_ms, sample.temp_c if sample else None, sample.freq_mhz if sample else None,
                 sample.load1 if sample else None, json.dumps(procs, ensure_ascii=False)),
            )
        return cur.lastrowid

    def end_event(self, event_id: int, end_ms: int, seconds: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE events SET end_ms=?, seconds=? WHERE id=?", (end_ms, seconds, event_id))

    def close_open_events(self) -> int:
        """진행 중으로 남은 이벤트를 해제 시각을 모르는 상태로 닫는다 (재시작·정전 뒤)."""
        with self.conn:
            cur = self.conn.execute("UPDATE events SET interrupted=1, end_ms=start_ms WHERE end_ms IS NULL")
        return cur.rowcount

    def apply_retention(self, now_ms: int, minute_days: int, event_days: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM minutes WHERE ts_ms<?", (now_ms - minute_days * DAY_MS,))
            self.conn.execute("DELETE FROM events WHERE start_ms<?", (now_ms - event_days * DAY_MS,))


class _MinuteBucket:
    def __init__(self, ts_ms: int):
        self.ts_ms = ts_ms
        self.temps: list[float] = []
        self.freqs: list[float] = []
        self.loads: list[float] = []
        self.cpus: list[float] = []
        self.mem_min: int | None = None
        self.undervolt = self.overheat = self.throttle = self.samples = 0

    def add(self, s: Sample, conditions: dict[str, bool], step_s: int) -> None:
        self.samples += 1
        if s.temp_c is not None:
            self.temps.append(s.temp_c)
        if s.freq_mhz is not None:
            self.freqs.append(s.freq_mhz)
        if s.load1 is not None:
            self.loads.append(s.load1)
        if s.cpu_pct is not None:
            self.cpus.append(s.cpu_pct)
        if s.mem_avail_mb is not None:
            self.mem_min = s.mem_avail_mb if self.mem_min is None else min(self.mem_min, s.mem_avail_mb)
        self.undervolt += step_s if conditions.get("undervoltage") else 0
        self.overheat += step_s if conditions.get("overheat") else 0
        if any(conditions.get(k) for k in ("throttled", "freq_capped", "soft_temp")):
            self.throttle += step_s

    def row(self, disk_free_gb: float | None) -> dict:
        avg = lambda v: round(sum(v) / len(v), 2) if v else None  # noqa: E731
        return {
            "ts_ms": self.ts_ms, "temp_avg": avg(self.temps),
            "temp_min": min(self.temps) if self.temps else None,
            "temp_max": max(self.temps) if self.temps else None,
            "freq_avg": avg(self.freqs), "load_avg": avg(self.loads),
            "cpu_avg": round(sum(self.cpus) / len(self.cpus), 1) if self.cpus else None,
            "mem_avail_mb": self.mem_min, "disk_free_gb": disk_free_gb,
            "undervolt_s": self.undervolt, "overheat_s": self.overheat, "throttle_s": self.throttle,
            "samples": self.samples,
        }


class Monitor:
    """표본을 받아 1분 요약과 이벤트를 만든다. 입출력은 reader, store 쪽에 있다."""

    def __init__(self, store: SysmonStore, settings: SysmonSettings, hz: int, capabilities: dict | None = None,
                 disk=lambda: None):
        self.store = store
        self.settings = settings
        self.hz = hz
        self.capabilities = capabilities or {}
        self.disk = disk
        self.ring: deque[tuple[int, dict[str, int]]] = deque(maxlen=max(1, math.ceil(PROC_WINDOW_S / settings.interval_s)))
        self.prev: dict | None = None
        self.bucket: _MinuteBucket | None = None
        self.pending: list[dict] = []
        self.last_flush_ms: int | None = None
        self.last_retention_ms: int | None = None
        self.active: dict[str, dict] = {}
        self.disk_free: float | None = None
        self.cpu_recent: deque[tuple[int, float]] = deque()

    def cpu_pct_1m(self) -> float | None:
        """최근 60초 표본의 CPU 사용률 평균."""
        if not self.cpu_recent:
            return None
        return round(sum(v for _, v in self.cpu_recent) / len(self.cpu_recent), 1)

    def begin(self, boot_id: str | None, now_ms: int, uptime_s: int | None) -> None:
        """시작 처리: 끝나지 못한 이벤트를 닫고, 새 부팅이면 기록한다."""
        self.store.close_open_events()
        if boot_id and self.store.get_meta("boot_id") != boot_id:
            start = now_ms - (uptime_s or 0) * 1000
            self.store.end_event(self.store.start_event("boot", start, None, []), start, 0)
            self.store.set_meta("boot_id", boot_id)

    def conditions(self, s: Sample) -> dict[str, bool]:
        out = {
            "undervoltage": bool(s.undervoltage), "throttled": bool(s.throttled),
            "freq_capped": bool(s.freq_capped), "soft_temp": bool(s.soft_temp),
            "overheat": s.temp_c is not None and s.temp_c >= self.settings.overheat_c,
        }
        return out

    def _record_processes(self, ts_ms: int, procs: dict) -> None:
        if self.prev is not None:
            deltas: dict[str, int] = defaultdict(int)
            for key, (label, ticks) in procs.items():
                before = self.prev.get(key)
                # 이번 주기에 새로 나타난 프로세스는 누적값 전체가 이번 주기 사용량이다
                delta = ticks - before[1] if before is not None else ticks
                if delta > 0:
                    deltas[label] += delta
            self.ring.append((ts_ms, dict(deltas)))
        self.prev = procs

    def top_processes(self, limit: int = TOP_PROCESSES) -> list[dict]:
        totals: dict[str, int] = defaultdict(int)
        for _, deltas in self.ring:
            for label, ticks in deltas.items():
                totals[label] += ticks
        window = max(len(self.ring) * self.settings.interval_s, 1)
        ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        return [{"name": name, "cpu_s": round(ticks / self.hz, 2), "pct": round(ticks / self.hz / window * 100, 1)}
                for name, ticks in ranked]

    def _update_events(self, s: Sample, conditions: dict[str, bool]) -> bool:
        started = False
        ts = s.ts_ms
        for kind, on in conditions.items():
            state = self.active.get(kind)
            if on:
                if state is None:
                    event_id = self.store.start_event(kind, ts, s, self.top_processes())
                    self.active[kind] = {"id": event_id, "last": ts, "seconds": self.settings.interval_s}
                    started = True
                else:
                    state["last"] = ts
                    state["seconds"] += self.settings.interval_s
            elif state is not None and ts - state["last"] >= CLEAR_AFTER_MS:
                self.store.end_event(state["id"], state["last"], state["seconds"])
                del self.active[kind]
        return started

    def flush(self) -> None:
        self.store.add_minutes(self.pending)
        self.pending = []

    def step(self, s: Sample, procs: dict) -> None:
        if self.last_flush_ms is None:
            self.last_flush_ms = s.ts_ms
        self._record_processes(s.ts_ms, procs)
        if s.cpu_pct is not None:
            self.cpu_recent.append((s.ts_ms, s.cpu_pct))
        while self.cpu_recent and s.ts_ms - self.cpu_recent[0][0] >= MINUTE_MS:
            self.cpu_recent.popleft()
        conditions = self.conditions(s)
        started = self._update_events(s, conditions)

        minute = s.ts_ms - s.ts_ms % MINUTE_MS
        if self.bucket is None or self.bucket.ts_ms != minute:
            if self.bucket is not None and self.bucket.samples:
                self.pending.append(self.bucket.row(self.disk_free))
            self.bucket = _MinuteBucket(minute)
            free = self.disk()
            self.disk_free = round(free[0], 2) if free else None
        self.bucket.add(s, conditions, self.settings.interval_s)

        if started or s.ts_ms - self.last_flush_ms >= self.settings.flush_s * 1000:
            self.flush()
            self.last_flush_ms = s.ts_ms
        if self.last_retention_ms is None or s.ts_ms - self.last_retention_ms >= RETENTION_CHECK_MS:
            self.store.apply_retention(s.ts_ms, self.settings.minute_retention_days, self.settings.event_retention_days)
            self.last_retention_ms = s.ts_ms

    def snapshot(self, s: Sample) -> dict:
        """화면의 현재값. tmpfs 파일로 나간다."""
        free = self.disk()
        out = asdict(s)
        out["cpu_pct_1m"] = self.cpu_pct_1m()
        out["active"] = sorted(self.active)
        out["top"] = self.top_processes(5)
        out["disk_free_gb"] = round(free[0], 2) if free else None
        out["disk_total_gb"] = round(free[1], 2) if free else None
        out["capabilities"] = self.capabilities
        out["pending"] = list(self.pending)
        if self.bucket is not None and self.bucket.samples:
            out["pending"].append(self.bucket.row(self.disk_free))
        return out

    def stop(self) -> None:
        if self.bucket is not None and self.bucket.samples:
            self.pending.append(self.bucket.row(self.disk_free))
            self.bucket = None
        self.flush()
        for state in self.active.values():
            self.store.end_event(state["id"], state["last"], state["seconds"])
        self.active.clear()


def write_runtime(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")
    os.replace(tmp, path)


def _clock_ticks() -> int:
    if os.name != "posix":
        raise ConfigError("sysmon 은 Linux 에서만 실행합니다")
    return os.sysconf("SC_CLK_TCK")


def run_monitor(db_path: Path, settings: SysmonSettings, stop: threading.Event,
                reader: SystemReader | None = None, clock=time.time) -> int:
    path = sysmon_db_path(db_path)
    reader = reader or SystemReader(disk_path=Path(db_path).parent)
    hz = _clock_ticks()
    store = SysmonStore(path)
    monitor = Monitor(store, settings, hz, reader.capabilities, reader.disk)
    runtime = Path(settings.runtime_file)
    runtime_ok = True
    if not reader.capabilities["undervoltage"]:
        log.warning("전압 상태를 읽을 수 없습니다 (get_throttled, rpi_volt hwmon 모두 없음)")
    first = reader.sample(int(clock() * 1000))
    monitor.begin(reader.boot_id(), first.ts_ms, first.uptime_s)
    log.info("시스템 모니터 시작 (주기 %ds, 전압 읽기: %s)", settings.interval_s, reader.capabilities)
    next_at = time.monotonic()
    try:
        while not stop.is_set():
            sample = reader.sample(int(clock() * 1000))
            monitor.step(sample, reader.processes(time.monotonic()))
            try:
                write_runtime(runtime, monitor.snapshot(sample))
                runtime_ok = True
            except OSError as e:
                if runtime_ok:
                    log.warning("현재값 파일을 쓰지 못했습니다: %s", e)
                runtime_ok = False
            next_at += settings.interval_s
            stop.wait(max(0.0, next_at - time.monotonic()))
    finally:
        monitor.stop()
        store.close()
    log.info("시스템 모니터 종료")
    return 0
