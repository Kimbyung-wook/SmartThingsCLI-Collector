"""원본 측정값을 시간·일·주 단위로 집계한다 (PLAN §7).

- 각 값은 다음 보고까지(최대 max_hold) 유지된 것으로 보고 시간 가중 평균을 낸다.
- min/max 는 그 구간에 실제로 유효했던 보고값 기준이다. 구간 시작 시점에 유지되던
  이전 보고값도 포함하므로 항상 min <= avg <= max 이다.
- 끝난 시간(정시까지)만 집계한다. 진행 중인 시간은 다음 실행에서 처리한다.
- 일·주는 시간 집계에서 파생한다. 경계는 KST (주는 월요일 시작).
- 다시 계산해도 결과가 같다. 값이 그대로면 일·주 행은 쓰지 않는다 (microSD 쓰기 절약).
"""

from __future__ import annotations

import bisect
import logging
import math
import time
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

from .settings import RollupSettings

log = logging.getLogger(__name__)

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
KST_OFFSET_MS = 9 * HOUR_MS

LUX_EDGES = (0, 1, 5, 10, 25, 50, 100, 200, 500, 1_000, 2_000, 5_000, 10_000)
LUX_LABELS = (
    "0–1", "1–5", "5–10", "10–25", "25–50", "50–100", "100–200",
    "200–500", "500–1k", "1k–2k", "2k–5k", "5k–10k", "10k+",
)

# 상태 값을 숫자로 바꿔 시간 가중 평균을 내는 속성 (평균 = 켜져 있던 비율)
STATE_MAPS: dict[str, dict[str, float]] = {
    "switch": {"on": 1.0, "off": 0.0},
    "motion": {"active": 1.0, "inactive": 0.0},
    "presence": {"present": 1.0, "not present": 0.0},
}
ROLLUP_ATTRIBUTES = ("temperature", "humidity", "illuminance", "power", "energy", "switch")
OCCUPANCY_ATTRIBUTES = ("presence", "motion")

_RETENTION_INTERVAL_MS = DAY_MS


def floor_hour(ms: int) -> int:
    return ms - ms % HOUR_MS


def kst_day_start(ms: int) -> int:
    return (ms + KST_OFFSET_MS) // DAY_MS * DAY_MS - KST_OFFSET_MS


def kst_week_start(ms: int) -> int:
    day = kst_day_start(ms)
    days_since_epoch = (day + KST_OFFSET_MS) // DAY_MS
    weekday = (days_since_epoch + 3) % 7  # 1970-01-01 은 목요일, 월요일 = 0
    return day - weekday * DAY_MS


def lux_bin(value: float) -> int:
    if value <= 0:
        return 0
    return bisect.bisect_right(LUX_EDGES, value) - 1


@dataclass(frozen=True)
class Series:
    device_id: str
    component: str
    capability: str
    attribute: str

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.device_id, self.component, self.capability, self.attribute)


@dataclass
class RollupStats:
    closed_until_ms: int = 0
    series: int = 0
    hours_written: int = 0
    days_touched: int = 0
    weeks_touched: int = 0
    deleted_readings: int = 0
    full: bool = False
    extra: dict = field(default_factory=dict)


Segment = tuple[int, int, float, bool]  # (시작, 끝, 값, 시작 시각이 이 범위 안의 보고인지)


def iter_segments(conn, s: Series, start: int, end: int, hold_ms: int) -> Iterator[Segment]:
    """[start, end) 안에서 값이 유지된 구간을 시간순으로 돌려준다."""
    params = (s.device_id, s.component, s.capability, s.attribute)
    head = "SELECT ts_ms, value_num, value_text FROM readings " \
           "WHERE device_id=? AND component=? AND capability=? AND attribute=? "
    state_map = STATE_MAPS.get(s.attribute)

    def value_of(num, text):
        if state_map is not None:
            return state_map.get(text)
        return num if num is not None and math.isfinite(num) else None

    prev = conn.execute(head + "AND ts_ms<? ORDER BY ts_ms DESC LIMIT 1", (*params, start)).fetchone()
    current_ts = current_val = None
    current_reported = False
    if prev is not None:
        current_ts, current_val = prev[0], value_of(prev[1], prev[2])

    def emit(next_ts: int):
        if current_ts is None or current_val is None:
            return None
        seg_start = max(current_ts, start)
        seg_end = min(next_ts, current_ts + hold_ms, end)
        return (seg_start, seg_end, current_val, current_reported) if seg_end > seg_start else None

    cur = conn.execute(head + "AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms", (*params, start, end))
    for ts, num, text in cur:
        seg = emit(ts)
        if seg:
            yield seg
        current_ts, current_val, current_reported = ts, value_of(num, text), True
    seg = emit(end)
    if seg:
        yield seg


def aggregate_hours(segments) -> dict[int, list]:
    """시간별 [유지ms, 가중합, min, max, last, 보고건수]."""
    hours: dict[int, list] = {}
    for start, end, value, reported in segments:
        if reported:
            hours.setdefault(floor_hour(start), [0, 0.0, value, value, value, 0])[5] += 1
        t = start
        while t < end:
            hour = t - t % HOUR_MS
            stop = min(end, hour + HOUR_MS)
            b = hours.get(hour)
            if b is None:
                b = hours[hour] = [0, 0.0, value, value, value, 0]
            dur = stop - t
            b[0] += dur
            b[1] += value * dur
            if value < b[2]:
                b[2] = value
            if value > b[3]:
                b[3] = value
            b[4] = value
            t = stop
    return hours


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for a, b in sorted(ranges):
        if a >= b:
            continue
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _hours_in(ranges: list[tuple[int, int]]) -> Iterator[int]:
    for a, b in ranges:
        yield from range(a, b, HOUR_MS)


def _primary_series(conn) -> tuple[dict[tuple[str, str], Series], list[Series]]:
    """(기기, 속성)마다 대표 측정 항목 하나를 고른다. main 컴포넌트 우선."""
    grouped: dict[tuple[str, str], list[Series]] = defaultdict(list)
    for r in conn.execute("SELECT device_id, component, capability, attribute FROM rollup_series"):
        grouped[(r[0], r[3])].append(Series(r[0], r[1], r[2], r[3]))
    primary = {}
    for key, items in grouped.items():
        primary[key] = min(items, key=lambda s: (s.component != "main", s.component, s.capability))
    return primary, list(primary.values())


def _occupancy_series(primary: dict[tuple[str, str], Series]) -> list[Series]:
    """기기마다 재실 항목 하나. presence 가 있으면 motion 보다 우선한다."""
    chosen: dict[str, Series] = {}
    for (device, attr), s in primary.items():
        if attr == "presence" or (attr == "motion" and device not in chosen):
            chosen[device] = s
    return list(chosen.values())


def lux_pairs(conn, primary: dict[tuple[str, str], Series]) -> list[tuple[Series, Series]]:
    """재실 센서와 조도 센서 짝. 같은 기기를 먼저, 없으면 같은 방의 첫 조도 센서."""
    rooms = {r[0]: r[1] for r in conn.execute("SELECT device_id, room_id FROM devices")}
    lux_by_id = {d: s for (d, a), s in primary.items() if a == "illuminance"}
    pairs = []
    for occ in _occupancy_series(primary):
        match = lux_by_id.get(occ.device_id)
        room = rooms.get(occ.device_id)
        if match is None and room:
            for dev_id in sorted(lux_by_id):
                if rooms.get(dev_id) == room:
                    match = lux_by_id[dev_id]
                    break
        if match is not None:
            pairs.append((occ, match))
    return pairs


def _write_hours(conn, s: Series, ranges, hours: dict[int, list]) -> int:
    rows = []
    for hour, (covered, weighted, lo, hi, last, n) in hours.items():
        if covered <= 0:
            continue
        avg = max(lo, min(hi, weighted / covered))
        rows.append((s.device_id, s.attribute, hour, avg, lo, hi, last, n, covered / HOUR_MS))
    with conn:
        for a, b in ranges:
            conn.execute(
                "DELETE FROM rollup_hour WHERE device_id=? AND attribute=? AND bucket_start>=? AND bucket_start<?",
                (s.device_id, s.attribute, a, b),
            )
        conn.executemany(
            "INSERT INTO rollup_hour(device_id, attribute, bucket_start, avg, min, max, last_val, n, coverage) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def _write_lux_hist(conn, s: Series, ranges, segments_by_range) -> None:
    rows = []
    for segs in segments_by_range:
        seconds: dict[tuple[int, int], int] = defaultdict(int)
        for start, end, value, _ in segs:
            t = start
            bin_no = lux_bin(value)
            while t < end:
                hour = t - t % HOUR_MS
                stop = min(end, hour + HOUR_MS)
                seconds[(hour, bin_no)] += stop - t
                t = stop
        rows.extend((s.device_id, h, b, ms / 1000) for (h, b), ms in seconds.items())
    with conn:
        for a, b in ranges:
            conn.execute(
                "DELETE FROM lux_hist_hour WHERE device_id=? AND bucket_start>=? AND bucket_start<?",
                (s.device_id, a, b),
            )
        conn.executemany(
            "INSERT INTO lux_hist_hour(device_id, bucket_start, lux_bin, seconds) VALUES(?,?,?,?)", rows
        )


def _write_occupancy(conn, s: Series, ranges) -> None:
    rows = []
    for a, b, hours in ranges:
        for hour, (covered, weighted, *_rest) in hours.items():
            if covered > 0:
                rows.append((s.device_id, hour, weighted / 1000, covered / 1000))
    with conn:
        for a, b, _ in ranges:
            conn.execute(
                "DELETE FROM occupancy_hour WHERE device_id=? AND bucket_start>=? AND bucket_start<?",
                (s.device_id, a, b),
            )
        conn.executemany(
            "INSERT INTO occupancy_hour(device_id, bucket_start, occupied_seconds, known_seconds) "
            "VALUES(?,?,?,?)",
            rows,
        )


_UPSERT = (
    "INSERT INTO {table}(device_id, attribute, bucket_start, avg, min, max, last_val, n, coverage) "
    "VALUES(?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT(device_id, attribute, bucket_start) DO UPDATE SET "
    "avg=excluded.avg, min=excluded.min, max=excluded.max, last_val=excluded.last_val, "
    "n=excluded.n, coverage=excluded.coverage "
    "WHERE (avg, min, max, last_val, n, coverage) IS NOT "
    "(excluded.avg, excluded.min, excluded.max, excluded.last_val, excluded.n, excluded.coverage)"
)


def _derive(conn, table: str, device: str, attribute: str, start: int, end: int, closed: int) -> None:
    """시간 집계에서 일·주 집계를 만든다. 경과 시간 대비 coverage."""
    rows = conn.execute(
        "SELECT avg, min, max, last_val, n, coverage FROM rollup_hour "
        "WHERE device_id=? AND attribute=? AND bucket_start>=? AND bucket_start<? ORDER BY bucket_start",
        (device, attribute, start, end),
    ).fetchall()
    with conn:
        if not rows:
            conn.execute(
                f"DELETE FROM {table} WHERE device_id=? AND attribute=? AND bucket_start=?",
                (device, attribute, start),
            )
            return
        covered = sum(r[5] for r in rows) * HOUR_MS
        lo = min(r[1] for r in rows)
        hi = max(r[2] for r in rows)
        avg = max(lo, min(hi, sum(r[0] * r[5] for r in rows) * HOUR_MS / covered))
        elapsed = max(min(end, closed) - start, HOUR_MS)
        conn.execute(
            _UPSERT.format(table=table),
            (device, attribute, start, avg, lo, hi, rows[-1][3], sum(r[4] for r in rows),
             min(1.0, covered / elapsed)),
        )


def _derive_touched(conn, s: Series, ranges, closed: int, stats: RollupStats) -> None:
    days, weeks = set(), set()
    for hour in _hours_in(ranges):
        days.add(kst_day_start(hour))
        weeks.add(kst_week_start(hour))
    for day in sorted(days):
        _derive(conn, "rollup_day", s.device_id, s.attribute, day, day + DAY_MS, closed)
    for week in sorted(weeks):
        _derive(conn, "rollup_week", s.device_id, s.attribute, week, week + WEEK_MS, closed)
    stats.days_touched += len(days)
    stats.weeks_touched += len(weeks)


def _claim(conn, closed: int) -> None:
    """끝난 시간의 변경 표시를 처리 대상으로 옮긴다. 이후 도착분은 rollup_dirty 에 새로 쌓인다."""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO rollup_claimed SELECT * FROM rollup_dirty WHERE hour_ms<?", (closed,)
        )
        conn.execute("DELETE FROM rollup_dirty WHERE hour_ms<?", (closed,))


def _register_series(conn, *, scan_all: bool) -> None:
    if scan_all:
        known = conn.execute(
            "SELECT DISTINCT device_id, component, capability, attribute FROM readings"
        ).fetchall()
    else:
        known = conn.execute(
            "SELECT DISTINCT device_id, component, capability, attribute FROM rollup_claimed"
        ).fetchall()
    rows = []
    for r in known:
        unit = conn.execute(
            "SELECT unit FROM readings WHERE device_id=? AND component=? AND capability=? AND attribute=? "
            "ORDER BY ts_ms DESC LIMIT 1",
            tuple(r),
        ).fetchone()
        rows.append((*tuple(r), unit[0] if unit else None))
    with conn:
        conn.executemany(
            "INSERT INTO rollup_series(device_id, component, capability, attribute, unit) VALUES(?,?,?,?,?) "
            "ON CONFLICT(device_id, attribute, component, capability) DO UPDATE SET unit=excluded.unit "
            "WHERE unit IS NOT excluded.unit",
            rows,
        )


def _affected_ranges(conn, s: Series, hour_marks: list[int], hold_ms: int, closed: int, floor_ms: int):
    """변경된 시간마다, 그 값이 영향을 주는 뒤쪽 시간까지 계산 범위를 넓힌다."""
    ranges = []
    for hour in hour_marks:
        if hour >= closed or hour + HOUR_MS <= floor_ms:
            continue
        nxt = conn.execute(
            "SELECT ts_ms FROM readings WHERE device_id=? AND component=? AND capability=? AND attribute=? "
            "AND ts_ms>=? ORDER BY ts_ms LIMIT 1",
            (*s.key[:4], hour + HOUR_MS),
        ).fetchone()
        limit = hour + HOUR_MS + hold_ms
        if nxt is not None:
            limit = min(limit, nxt[0] + 1)
        stop = min(floor_hour(limit - 1) + HOUR_MS, closed)
        ranges.append((max(hour, floor_hour(floor_ms)), stop))
    return ranges


def _full_range(conn, s: Series, closed: int, floor_ms: int) -> tuple[int, int] | None:
    row = conn.execute(
        "SELECT MIN(ts_ms) FROM readings WHERE device_id=? AND component=? AND capability=? AND attribute=?",
        s.key,
    ).fetchone()
    if row[0] is None:
        return None
    return (max(floor_hour(row[0]), floor_hour(floor_ms)), closed)


def _apply_retention(db, settings: RollupSettings, now_ms: int) -> int:
    """보관 기간이 지난 원본을 지운다. 하루 한 번, 항목별 최신 보고는 현재 상태를 알기 위해 남긴다."""
    if settings.retention_days <= 0:
        return 0
    last = db.get_meta("rollup_retention_ms")
    if last is not None and now_ms - int(last) < _RETENTION_INTERVAL_MS:
        return 0
    conn = db.conn
    cutoff = now_ms - settings.retention_days * DAY_MS
    deleted = 0
    for r in conn.execute("SELECT device_id, component, capability, attribute FROM rollup_series").fetchall():
        key = tuple(r)
        newest = conn.execute(
            "SELECT ts_ms FROM readings WHERE device_id=? AND component=? AND capability=? AND attribute=? "
            "ORDER BY ts_ms DESC LIMIT 1", key,
        ).fetchone()
        if newest is None:
            continue
        with conn:
            deleted += conn.execute(
                "DELETE FROM readings WHERE device_id=? AND component=? AND capability=? AND attribute=? AND ts_ms<?",
                (*key, min(cutoff, newest[0])),
            ).rowcount
    db.set_meta("rollup_retention_ms", now_ms)
    return deleted


def _rollup_pairs(conn, pairs, touched: dict[tuple[str, str], list], settings: RollupSettings) -> None:
    for occ, lux in pairs:
        ranges = _merge(touched.get((occ.device_id, occ.attribute), [])
                        + touched.get((lux.device_id, lux.attribute), []))
        if not ranges:
            continue
        rows = []
        hold_occ, hold_lux = settings.hold_ms(occ.attribute), settings.hold_ms(lux.attribute)
        for a, b in ranges:
            occupied = [(st, en) for st, en, v, _ in iter_segments(conn, occ, a, b, hold_occ) if v > 0]
            lux_segs = list(iter_segments(conn, lux, a, b, hold_lux))
            seconds: dict[tuple[int, int], int] = defaultdict(int)
            j = 0
            for st, en in occupied:
                while j < len(lux_segs) and lux_segs[j][1] <= st:
                    j += 1
                k = j
                while k < len(lux_segs) and lux_segs[k][0] < en:
                    t, stop = max(st, lux_segs[k][0]), min(en, lux_segs[k][1])
                    bin_no = lux_bin(lux_segs[k][2])
                    while t < stop:
                        hour = t - t % HOUR_MS
                        piece = min(stop, hour + HOUR_MS)
                        seconds[(hour, bin_no)] += piece - t
                        t = piece
                    k += 1
            rows.extend((occ.device_id, lux.device_id, h, bin_no, ms / 1000)
                        for (h, bin_no), ms in seconds.items())
        with conn:
            for a, b in ranges:
                conn.execute(
                    "DELETE FROM lux_occ_hour WHERE occ_device_id=? AND lux_device_id=? "
                    "AND bucket_start>=? AND bucket_start<?",
                    (occ.device_id, lux.device_id, a, b),
                )
            conn.executemany(
                "INSERT INTO lux_occ_hour(occ_device_id, lux_device_id, bucket_start, lux_bin, seconds) "
                "VALUES(?,?,?,?,?)",
                rows,
            )


def run_rollup(db, settings: RollupSettings | None = None, now_ms: int | None = None,
               full: bool = False) -> RollupStats:
    """끝난 시간까지 집계한다. 필요한 구간만 다시 계산한다."""
    settings = settings or RollupSettings()
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    conn = db.conn
    closed = floor_hour(now_ms)
    stats = RollupStats(closed_until_ms=closed)
    floor_ms = 0
    if settings.retention_days > 0:
        floor_ms = now_ms - settings.retention_days * DAY_MS

    stored = db.get_meta("rollup_closed_until_ms")
    prev_closed = int(stored) if stored is not None else None
    full = full or prev_closed is None
    stats.full = full

    _claim(conn, closed)
    _register_series(conn, scan_all=full)
    primary, series = _primary_series(conn)
    occupancy = _occupancy_series(primary)
    occupancy_keys = {s.key for s in occupancy}
    rollup_keys = {s.key for s in series if s.attribute in ROLLUP_ATTRIBUTES}

    claimed: dict[tuple, list[int]] = defaultdict(list)
    if not full:
        for r in conn.execute(
            "SELECT device_id, component, capability, attribute, hour_ms FROM rollup_claimed ORDER BY hour_ms"
        ):
            claimed[tuple(r[:4])].append(r[4])

    touched: dict[tuple[str, str], list] = defaultdict(list)
    for s in series:
        if s.key not in rollup_keys and s.key not in occupancy_keys:
            continue
        hold_ms = settings.hold_ms(s.attribute)
        if full:
            whole = _full_range(conn, s, closed, floor_ms)
            ranges = [whole] if whole else []
        else:
            ranges = _affected_ranges(conn, s, claimed.get(s.key, []), hold_ms, closed, floor_ms)
            if prev_closed is not None and prev_closed < closed:
                ranges.append((prev_closed, closed))
        ranges = _merge(ranges)
        if not ranges:
            continue
        stats.series += 1
        touched[(s.device_id, s.attribute)].extend(ranges)

        if s.key in rollup_keys:
            segs_list = []
            hours: dict[int, list] = {}
            keep_segments = s.attribute == "illuminance"
            for a, b in ranges:
                segs = iter_segments(conn, s, a, b, hold_ms)
                if keep_segments:
                    segs = list(segs)
                    segs_list.append(segs)
                hours.update(aggregate_hours(segs))
            stats.hours_written += _write_hours(conn, s, ranges, hours)
            if keep_segments:
                _write_lux_hist(conn, s, ranges, segs_list)
            _derive_touched(conn, s, ranges, closed, stats)
        if s.key in occupancy_keys:
            computed = []
            for a, b in ranges:
                computed.append((a, b, aggregate_hours(iter_segments(conn, s, a, b, hold_ms))))
            _write_occupancy(conn, s, computed)

    _rollup_pairs(conn, lux_pairs(conn, primary), touched, settings)

    with conn:
        conn.execute("DELETE FROM rollup_claimed")
    db.set_meta("rollup_closed_until_ms", closed)
    db.set_meta("rollup_finished_ms", int(time.time() * 1000))
    stats.deleted_readings = _apply_retention(db, settings, now_ms)
    return stats
