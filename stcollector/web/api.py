"""대시보드 JSON API. 차트는 집계 표에서만 읽는다.

원본(readings)은 기기·속성별 최신값 조회와, 아직 집계되지 않은 최근 구간의
오늘 최대/최소 계산에만 쓴다. 둘 다 기본 키 범위 조회라서 표를 훑지 않는다.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sqlite3
import time
from collections import defaultdict

from .. import __version__
from pathlib import Path

from ..rollup import (
    DAY_MS, HOUR_MS, KST_OFFSET_MS, LUX_LABELS, WEEK_MS, floor_hour, kst_day_start, kst_week_start,
)

ROLLUP_TABLES = {"hour": "rollup_hour", "day": "rollup_day", "week": "rollup_week"}
TREND_PERIODS = {"24h": ("hour", 24), "month": ("day", 30), "year": ("week", 52)}
TREND_METRICS = {"temperature", "humidity", "illuminance", "dewpoint"}
DIST_PERIODS = {"week": 7, "month": 30, "year": 365}
OCC_PERIODS = {"month": 30, "year": 365}
PLUG_PERIODS = {"month", "year"}
_DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

ENV_ATTRIBUTES = ("temperature", "humidity", "illuminance")
SENSOR_ATTRIBUTES = (*ENV_ATTRIBUTES, "motion", "presence")
STALE_COLLECT_MS = 30 * 60 * 1000
# 이력 조회 실패가 한두 번의 일시 오류보다 오래 이어질 때만 경고한다 (수집 주기 5분의 2배).
STALE_HISTORY_MS = 10 * 60 * 1000
STALE_ROLLUP_MS = 2 * HOUR_MS + 10 * 60 * 1000
LOW_BATTERY = 20
LOW_DISK_BYTES = 1024**3
SYSMON_PERIODS = {"24h": (5 * 60 * 1000, 288), "1h": (60 * 1000, 60)}  # (구간 ms, 구간 수)
SYSMON_STALE_MS = 30_000
SYSMON_EVENT_LIMIT = 100
SYSMON_ALERT_KINDS = {
    "undervoltage": ("critical", "Raspberry Pi 전압 부족이 감지되고 있습니다. 상태 탭에서 직전 작업을 볼 수 있습니다."),
    "throttled": ("warning", "Raspberry Pi CPU 클럭이 전원·온도 때문에 제한되고 있습니다."),
    "overheat": ("critical", "Raspberry Pi 온도가 과열 기준을 넘었습니다."),
}


class BadRequest(Exception):
    pass


def _r(value, digits=2):
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _choice(params: dict, name: str, allowed, default=None) -> str:
    value = params.get(name, default)
    if value not in allowed:
        raise BadRequest(f"{name} 값이 올바르지 않습니다")
    return value


class Series:
    __slots__ = ("device_id", "component", "capability", "attribute", "unit", "label", "room", "room_id")

    def __init__(self, row):
        self.device_id, self.component, self.capability, self.attribute = row[0], row[1], row[2], row[3]
        self.unit, self.label, self.room, self.room_id = row[4], row[5] or row[0], row[6], row[7]

    @property
    def key(self):
        return (self.device_id, self.component, self.capability, self.attribute)

    @property
    def place(self):
        return self.room or self.label


class Context:
    def __init__(self, conn, now_ms: int, db_path: Path, sysmon_db: Path | None = None,
                 sysmon_current: Path | None = None):
        self.conn, self.now_ms, self.db_path = conn, now_ms, db_path
        self.sysmon_db, self.sysmon_current = sysmon_db, sysmon_current
        self._index = None

    def meta(self, key: str):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def meta_int(self, key: str):
        value = self.meta(key)
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None

    @property
    def closed_until(self):
        return self.meta_int("rollup_closed_until_ms")

    @property
    def index(self) -> dict[str, list[Series]]:
        if self._index is None:
            best: dict[tuple[str, str], Series] = {}
            for row in self.conn.execute(
                "SELECT s.device_id, s.component, s.capability, s.attribute, s.unit, d.label, d.room_name, d.room_id "
                "FROM rollup_series s LEFT JOIN devices d USING(device_id)"
            ):
                s = Series(row)
                key = (s.device_id, s.attribute)
                cur = best.get(key)
                if cur is None or (s.component != "main", s.component, s.capability) < (
                    cur.component != "main", cur.component, cur.capability
                ):
                    best[key] = s
            index: dict[str, list[Series]] = defaultdict(list)
            for (_, attr), s in best.items():
                index[attr].append(s)
            for items in index.values():
                items.sort(key=lambda s: (s.place, s.label))
            self._index = index
        return self._index

    def rooms(self) -> list[str]:
        names = {s.place for attr in SENSOR_ATTRIBUTES for s in self.index.get(attr, [])}
        return sorted(names)

    def color(self, place: str, rooms: list[str] | None = None):
        rooms = rooms if rooms is not None else self.rooms()
        try:
            i = rooms.index(place)
        except ValueError:
            return None
        return i if i < 8 else None  # 범주 색은 8개까지만, 나머지는 회색

    def occupancy_series(self) -> list[Series]:
        chosen: dict[str, Series] = {}
        for attr in ("motion", "presence"):  # presence 가 나중에 덮어써서 우선한다
            for s in self.index.get(attr, []):
                chosen[s.device_id] = s
        return sorted(chosen.values(), key=lambda s: (s.place, s.label))

    def latest(self, s: Series):
        return self.conn.execute(
            "SELECT ts_ms, value_num, value_text FROM readings "
            "WHERE device_id=? AND component=? AND capability=? AND attribute=? ORDER BY ts_ms DESC LIMIT 1",
            s.key,
        ).fetchone()

    def device_param(self, params: dict, attr_candidates) -> Series | None:
        raw = params.get("device")
        if raw is None:
            return None
        if not _DEVICE_RE.match(raw):
            raise BadRequest("device 값이 올바르지 않습니다")
        for attr in attr_candidates:
            for s in self.index.get(attr, []):
                if s.device_id == raw:
                    return s
        raise BadRequest("알 수 없는 device 입니다")


def dew_point(temp_c: float, rh: float):
    if rh is None or rh <= 0:
        return None
    gamma = math.log(rh / 100) + 17.625 * temp_c / (243.04 + temp_c)
    return 243.04 * gamma / (17.625 - gamma)


def _to_celsius(value, unit):
    return (value - 32) * 5 / 9 if unit == "F" else value


# ---- /api/series ------------------------------------------------------------
def _buckets(ctx: Context, period: str) -> tuple[str, list[int]]:
    grain, count = TREND_PERIODS[period]
    now = ctx.now_ms
    if grain == "hour":
        end = ctx.closed_until if ctx.closed_until is not None else floor_hour(now)
        return grain, [end - (count - i) * HOUR_MS for i in range(count)]
    if grain == "day":
        today = kst_day_start(now)
        return grain, [today - (count - 1 - i) * DAY_MS for i in range(count)]
    week = kst_week_start(now)
    return grain, [week - (count - 1 - i) * WEEK_MS for i in range(count)]


def _rows_by_bucket(ctx, table, device, attribute, start):
    return {
        r[0]: r for r in ctx.conn.execute(
            f"SELECT bucket_start, avg, min, max, coverage FROM {table} "
            "WHERE device_id=? AND attribute=? AND bucket_start>=? ORDER BY bucket_start",
            (device, attribute, start),
        )
    }


def series(ctx: Context, params: dict) -> dict:
    metric = _choice(params, "metric", TREND_METRICS, "temperature")
    period = _choice(params, "period", TREND_PERIODS, "24h")
    grain, buckets = _buckets(ctx, period)
    table = ROLLUP_TABLES[grain]
    start = buckets[0]
    rooms = ctx.rooms()
    out = []
    digits = 0 if metric == "illuminance" else 1

    if metric == "dewpoint":
        humidity = ctx.index.get("humidity", [])
        for t in ctx.index.get("temperature", []):
            h = next((x for x in humidity if x.device_id == t.device_id), None) or next(
                (x for x in humidity if x.room_id and x.room_id == t.room_id), None
            )
            if h is None:
                continue
            tr = _rows_by_bucket(ctx, table, t.device_id, "temperature", start)
            hr = _rows_by_bucket(ctx, table, h.device_id, "humidity", start)
            avg, cov = [], []
            for b in buckets:
                a, c = tr.get(b), hr.get(b)
                avg.append(_r(dew_point(_to_celsius(a[1], t.unit), c[1]), 1) if a and c else None)
                cov.append(_r(min(a[4], c[4]), 2) if a and c else None)
            out.append({"id": t.device_id, "name": t.place, "color": ctx.color(t.place, rooms),
                        "avg": avg, "min": None, "max": None, "coverage": cov})
        unit = "°C"
    else:
        items = ctx.index.get(metric, [])
        for s in items:
            rows = _rows_by_bucket(ctx, table, s.device_id, metric, start)
            avg, lo, hi, cov = [], [], [], []
            for b in buckets:
                row = rows.get(b)
                avg.append(_r(row[1], digits) if row else None)
                lo.append(_r(row[2], digits) if row else None)
                hi.append(_r(row[3], digits) if row else None)
                cov.append(_r(row[4], 2) if row else None)
            out.append({"id": s.device_id, "name": s.place, "color": ctx.color(s.place, rooms),
                        "avg": avg, "min": lo, "max": hi, "coverage": cov})
        unit = {"temperature": "°C", "humidity": "%", "illuminance": "lx"}[metric]
    return {"metric": metric, "period": period, "grain": grain, "unit": unit, "digits": digits,
            "buckets": buckets, "closed_until": ctx.closed_until, "series": out}


# ---- /api/lux-dist ----------------------------------------------------------
def most_time_range(values: list[float], share: float = 0.8):
    """share 이상을 담는 가장 좁은 연속 구간. 폭이 같으면 더 많이 담는 쪽."""
    total = sum(values)
    if total <= 0:
        return None
    best = None
    for i in range(len(values)):
        acc = 0.0
        for j in range(i, len(values)):
            acc += values[j]
            if acc >= share * total - 1e-9:
                key = (j - i, -acc)
                if best is None or key < best[0]:
                    best = (key, i, j, acc)
                break
    return {"from": best[1], "to": best[2], "share": _r(best[3] / total, 3)}


def _occ_for_lux(ctx: Context, lux: Series) -> Series | None:
    candidates = ctx.occupancy_series()
    return next((o for o in candidates if o.device_id == lux.device_id), None) or next(
        (o for o in candidates if o.room_id and o.room_id == lux.room_id), None
    )


def lux_dist(ctx: Context, params: dict) -> dict:
    period = _choice(params, "period", DIST_PERIODS, "month")
    scope = _choice(params, "scope", {"all", "occupied"}, "all")
    sensors = ctx.index.get("illuminance", [])
    s = ctx.device_param(params, ("illuminance",))
    if s is None:
        if not sensors:
            return {"period": period, "scope": scope, "bins": LUX_LABELS, "device": None, "has_data": False}
        s = sensors[0]
    occ = _occ_for_lux(ctx, s)
    days = DIST_PERIODS[period]
    today = kst_day_start(ctx.now_ms)
    start = today - (days - 1) * DAY_MS

    if scope == "occupied" and occ is not None:
        rows = ctx.conn.execute(
            "SELECT ((bucket_start+?)%86400000)/3600000 AS h, lux_bin, SUM(seconds) FROM lux_occ_hour "
            "WHERE occ_device_id=? AND lux_device_id=? AND bucket_start>=? GROUP BY h, lux_bin",
            (KST_OFFSET_MS, occ.device_id, s.device_id, start),
        ).fetchall()
    elif scope == "occupied":
        rows = []
    else:
        rows = ctx.conn.execute(
            "SELECT ((bucket_start+?)%86400000)/3600000 AS h, lux_bin, SUM(seconds) FROM lux_hist_hour "
            "WHERE device_id=? AND bucket_start>=? GROUP BY h, lux_bin",
            (KST_OFFSET_MS, s.device_id, start),
        ).fetchall()

    matrix = [[0.0] * 24 for _ in LUX_LABELS]
    totals = [0.0] * len(LUX_LABELS)
    for hour, bin_no, seconds in rows:
        hours = seconds / 3600
        matrix[bin_no][hour] = _r(hours, 2)
        totals[bin_no] += hours
    sum_hours = sum(totals)
    mode = max(range(len(totals)), key=totals.__getitem__) if sum_hours > 0 else None

    stat = ctx.conn.execute(
        "SELECT MIN(min), MAX(max), SUM(avg*coverage), SUM(coverage) FROM rollup_day "
        "WHERE device_id=? AND attribute='illuminance' AND bucket_start>=?",
        (s.device_id, start),
    ).fetchone()
    mean = stat[2] / stat[3] if stat[3] else None

    # 날짜 × 시각: 7·30일은 날짜별, 1년은 주별로 묶는다.
    weekly = period == "year"
    if weekly:
        week = kst_week_start(ctx.now_ms)
        columns = [week - (51 - i) * WEEK_MS for i in range(52)]
        first = columns[0]
    else:
        columns = [today - (days - 1 - i) * DAY_MS for i in range(days)]
        first = start
    column_ms = WEEK_MS if weekly else DAY_MS
    grid = [[[0.0, 0.0] for _ in range(24)] for _ in columns]
    for bucket, avg, cov in ctx.conn.execute(
        "SELECT bucket_start, avg, coverage FROM rollup_hour "
        "WHERE device_id=? AND attribute='illuminance' AND bucket_start>=?",
        (s.device_id, first),
    ):
        c = (bucket - first) // column_ms
        if 0 <= c < len(columns):
            h = ((bucket + KST_OFFSET_MS) % DAY_MS) // HOUR_MS
            grid[c][h][0] += avg * cov
            grid[c][h][1] += cov
    dates = [[_r(a / w, 1) if w > 0 else None for a, w in col] for col in grid]

    return {
        "period": period, "scope": scope, "bins": LUX_LABELS,
        "device": {"id": s.device_id, "name": s.place},
        "occupancy_available": occ is not None,
        "has_data": sum_hours > 0,
        "totals_hours": [_r(v, 2) for v in totals],
        "total_hours": _r(sum_hours, 1),
        "mode_bin": mode,
        "range80": most_time_range(totals),
        "matrix": matrix,
        "avg": _r(mean, 1), "min": _r(stat[0], 1), "max": _r(stat[1], 1),
        "date_columns": columns, "date_grain": "week" if weekly else "day", "dates": dates,
    }


# ---- /api/occupancy ----------------------------------------------------------
def occupancy(ctx: Context, params: dict) -> dict:
    period = _choice(params, "period", OCC_PERIODS, "month")
    start = kst_day_start(ctx.now_ms) - (OCC_PERIODS[period] - 1) * DAY_MS
    lux_sensors = ctx.index.get("illuminance", [])
    devices = []
    for s in ctx.occupancy_series():
        rows = ctx.conn.execute(
            "SELECT ((((bucket_start+?)/86400000)+3)%7) AS wd, ((bucket_start+?)%86400000)/3600000 AS h, "
            "SUM(occupied_seconds), SUM(known_seconds) FROM occupancy_hour "
            "WHERE device_id=? AND bucket_start>=? GROUP BY wd, h",
            (KST_OFFSET_MS, KST_OFFSET_MS, s.device_id, start),
        ).fetchall()
        matrix = [[None] * 24 for _ in range(7)]
        occupied = known = 0.0
        peak = None
        for wd, h, occ, kn in rows:
            ratio = occ / kn if kn > 0 else None
            matrix[wd][h] = _r(ratio, 3)
            occupied += occ
            known += kn
            if ratio is not None and (peak is None or ratio > peak["ratio"]):
                peak = {"weekday": wd, "hour": h, "ratio": _r(ratio, 3)}
        lux = next((x for x in lux_sensors if x.device_id == s.device_id), None) or next(
            (x for x in lux_sensors if x.room_id and x.room_id == s.room_id), None
        )
        devices.append({
            "id": s.device_id, "name": s.place, "matrix": matrix, "peak": peak,
            "ratio": _r(occupied / known, 3) if known > 0 else None,
            "known_hours": _r(known / 3600, 1),
            "lux_device": lux.device_id if lux else None,
        })
    return {"period": period, "devices": devices}


# ---- /api/plugs ---------------------------------------------------------------
def _month_start(day_ms: int) -> int:
    """day_ms 가 속한 KST 달의 시작."""
    days = (day_ms + KST_OFFSET_MS) // DAY_MS
    y, m, d = _civil(days)
    return (days - d + 1) * DAY_MS - KST_OFFSET_MS


def _civil(days: int) -> tuple[int, int, int]:
    from datetime import date, timedelta
    day = date(1970, 1, 1) + timedelta(days=days)
    return day.year, day.month, day.day


def _prev_months(month_ms: int, count: int) -> list[int]:
    out = [month_ms]
    for _ in range(count - 1):
        out.append(_month_start(out[-1] - DAY_MS))
    return list(reversed(out))


def _to_kwh(value: float, unit) -> float:
    return value / 1000 if unit == "Wh" else value


def plugs(ctx: Context, params: dict) -> dict:
    period = _choice(params, "period", PLUG_PERIODS, "month")
    today = kst_day_start(ctx.now_ms)
    if period == "month":
        buckets = [today - (29 - i) * DAY_MS for i in range(30)]
        key_of = lambda d: d  # noqa: E731
    else:
        buckets = _prev_months(_month_start(today), 12)
        key_of = _month_start
    start = buckets[0]
    pos = {b: i for i, b in enumerate(buckets)}
    closed = ctx.closed_until or floor_hour(ctx.now_ms)
    energy_by_id = {s.device_id: s for s in ctx.index.get("energy", [])}
    power_by_id = {s.device_id: s for s in ctx.index.get("power", [])}

    def day_hours(day):
        return max(min(day + DAY_MS, closed) - day, 0) / HOUR_MS

    result = []
    total_kwh = [0.0] * len(buckets)
    any_energy = False
    for s in ctx.index.get("switch", []):
        on_hours = [0.0] * len(buckets)
        for day, avg, cov in ctx.conn.execute(
            "SELECT bucket_start, avg, coverage FROM rollup_day WHERE device_id=? AND attribute='switch' AND bucket_start>=?",
            (s.device_id, start),
        ):
            i = pos.get(key_of(day))
            if i is not None:
                on_hours[i] += avg * cov * day_hours(day)

        kwh = None
        estimated = False
        if s.device_id in energy_by_id:
            meter = energy_by_id[s.device_id]
            kwh = [0.0] * len(buckets)
            prev = ctx.conn.execute(
                "SELECT last_val FROM rollup_day WHERE device_id=? AND attribute='energy' AND bucket_start<? "
                "ORDER BY bucket_start DESC LIMIT 1", (s.device_id, start),
            ).fetchone()
            prev_last = prev[0] if prev else None
            for day, lo, last in ctx.conn.execute(
                "SELECT bucket_start, min, last_val FROM rollup_day WHERE device_id=? AND attribute='energy' "
                "AND bucket_start>=? ORDER BY bucket_start", (s.device_id, start),
            ):
                delta = last - prev_last if prev_last is not None else last - lo
                if delta < 0:  # 계량기 초기화
                    delta = last - lo
                prev_last = last
                i = pos.get(key_of(day))
                if i is not None:
                    kwh[i] += _to_kwh(delta, meter.unit)
        elif s.device_id in power_by_id:
            estimated = True
            kwh = [0.0] * len(buckets)
            for day, avg, cov in ctx.conn.execute(
                "SELECT bucket_start, avg, coverage FROM rollup_day WHERE device_id=? AND attribute='power' AND bucket_start>=?",
                (s.device_id, start),
            ):
                i = pos.get(key_of(day))
                if i is not None:
                    kwh[i] += avg * cov * day_hours(day) / 1000

        if kwh is not None:
            any_energy = True
            for i, v in enumerate(kwh):
                total_kwh[i] += v

        state = ctx.latest(s)
        power_now = None
        if s.device_id in power_by_id:
            latest_power = ctx.latest(power_by_id[s.device_id])
            power_now = _r(latest_power[1], 1) if latest_power else None
        today_on = ctx.conn.execute(
            "SELECT SUM(avg*coverage) FROM rollup_hour WHERE device_id=? AND attribute='switch' AND bucket_start>=?",
            (s.device_id, today),
        ).fetchone()[0]
        result.append({
            "id": s.device_id, "name": s.label, "room": s.room,
            "state": state[2] if state else None,
            "power_w": power_now,
            "today_on_hours": _r(today_on or 0.0, 1),
            "on_hours_total": _r(sum(on_hours), 1),
            "kwh_total": _r(sum(kwh), 2) if kwh is not None else None,
            "kwh": [_r(v, 3) for v in kwh] if kwh is not None else None,
            "estimated": estimated,
        })
    result.sort(key=lambda p: (-(p["kwh_total"] if p["kwh_total"] is not None else -1), -p["on_hours_total"]))
    return {"period": period, "buckets": buckets, "grain": "day" if period == "month" else "month",
            "plugs": result, "total_kwh": [_r(v, 3) for v in total_kwh] if any_energy else None}


# ---- /api/summary -------------------------------------------------------------
def _today_range(ctx: Context, s: Series, today: int):
    row = ctx.conn.execute(
        "SELECT MIN(min), MAX(max) FROM rollup_hour WHERE device_id=? AND attribute=? AND bucket_start>=?",
        (s.device_id, s.attribute, today),
    ).fetchone()
    lo, hi = row[0], row[1]
    since = max(ctx.closed_until or today, today)
    fresh = ctx.conn.execute(
        "SELECT MIN(value_num), MAX(value_num) FROM readings "
        "WHERE device_id=? AND component=? AND capability=? AND attribute=? AND ts_ms>=?",
        (*s.key, since),
    ).fetchone()
    if fresh[0] is not None:
        lo = fresh[0] if lo is None else min(lo, fresh[0])
        hi = fresh[1] if hi is None else max(hi, fresh[1])
    return lo, hi


def summary(ctx: Context, params: dict) -> dict:
    today = kst_day_start(ctx.now_ms)
    rooms = ctx.rooms()
    cards: dict[str, dict] = {r: {"name": r, "color": ctx.color(r, rooms)} for r in rooms}
    for attr in ENV_ATTRIBUTES:
        for s in ctx.index.get(attr, []):
            latest = ctx.latest(s)
            if latest is None or latest[1] is None:
                continue
            lo, hi = _today_range(ctx, s, today)
            digits = 0 if attr == "illuminance" else 1
            card = cards[s.place]
            card[attr] = {"now": _r(latest[1], digits), "ts": latest[0],
                          "today_min": _r(lo, digits), "today_max": _r(hi, digits)}
    for s in ctx.occupancy_series():
        latest = ctx.latest(s)
        if latest is not None:
            cards[s.place]["occupancy"] = {
                "occupied": latest[2] in ("active", "present"), "ts": latest[0],
            }
    out = []
    for card in cards.values():
        t, h = card.get("temperature"), card.get("humidity")
        if t and h:
            card["dew_point"] = _r(dew_point(t["now"], h["now"]), 1)
        out.append(card)
    return {"rooms": out, "closed_until": ctx.closed_until, "has_data": bool(out)}


# ---- /api/status, 경고 -----------------------------------------------------------
def _disk(ctx: Context) -> dict:
    usage = shutil.disk_usage(ctx.db_path.parent)
    size = 0
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(ctx.db_path) + suffix)
        if p.exists():
            size += p.stat().st_size
    return {"total": usage.total, "used": usage.used, "free": usage.free, "db_bytes": size}


def _batteries(ctx: Context) -> list[dict]:
    out = []
    for s in ctx.index.get("battery", []):
        latest = ctx.latest(s)
        if latest is not None and latest[1] is not None:
            out.append({"device_id": s.device_id, "name": s.label, "room": s.room,
                        "level": _r(latest[1], 0), "ts": latest[0]})
    return sorted(out, key=lambda b: b["level"])


def alerts(ctx: Context, disk: dict | None = None, batteries: list | None = None) -> list[dict]:
    out = []
    now = ctx.now_ms
    if ctx.meta("auth_state") == "reauth_required":
        out.append({"level": "critical", "text": "SmartThings 재인증이 필요합니다. RPi에서 stcollector auth 를 실행하세요."})
    last_ok = ctx.meta_int("last_ok_ms")
    if last_ok is not None and now - last_ok > STALE_COLLECT_MS:
        minutes = (now - last_ok) // 60000
        out.append({"level": "critical", "text": f"수집이 {minutes}분째 멈췄습니다."})
    fail_since = ctx.meta_int("history_fail_since_ms")
    if fail_since is not None and now - fail_since > STALE_HISTORY_MS:
        minutes = (now - fail_since) // 60000
        text = f"SmartThings 이력 조회가 {minutes}분째 실패하고 있습니다."
        fallback = ctx.meta_int("status_fallback_ms")
        if fallback is not None and fallback >= fail_since:
            text += " 그동안 현재값만 임시로 받고, 회복되면 밀린 이력을 다시 받습니다."
        out.append({"level": "warning", "text": text})
    finished = ctx.meta_int("rollup_finished_ms")
    if finished is not None and now - finished > STALE_ROLLUP_MS:
        out.append({"level": "warning", "text": "집계 작업이 2시간 넘게 실행되지 않았습니다."})
    disk = disk if disk is not None else _disk(ctx)
    if disk["free"] < LOW_DISK_BYTES:
        out.append({"level": "critical", "text": f"디스크 여유 공간이 {disk['free'] / 1024**3:.1f}GB 남았습니다."})
    for b in (batteries if batteries is not None else _batteries(ctx)):
        if b["level"] < LOW_BATTERY:
            out.append({"level": "warning", "text": f"{b['name']} 배터리 {b['level']:.0f}%"})
    current = _sysmon_current(ctx)
    if current is not None and now - int(current.get("ts_ms", 0)) <= SYSMON_STALE_MS:
        for kind in current.get("active") or []:
            if kind in SYSMON_ALERT_KINDS:
                level, text = SYSMON_ALERT_KINDS[kind]
                out.append({"level": level, "text": text})
    return out


def _gaps(ctx: Context, days: int = 14) -> list[dict]:
    """온습도·조도 센서가 모두 조용했던 시간 구간."""
    closed = ctx.closed_until
    if closed is None:
        return []
    start = closed - days * DAY_MS
    covered: set[int] = set()
    first = None
    for attr in ENV_ATTRIBUTES:
        for s in ctx.index.get(attr, []):
            for bucket, cov in ctx.conn.execute(
                "SELECT bucket_start, coverage FROM rollup_hour WHERE device_id=? AND attribute=? AND bucket_start>=?",
                (s.device_id, attr, start),
            ):
                if cov >= 0.5:
                    covered.add(bucket)
                    first = bucket if first is None else min(first, bucket)
    if first is None:
        return []
    gaps, run = [], None
    for hour in range(first, closed, HOUR_MS):
        if hour in covered:
            if run is not None:
                gaps.append({"start": run, "end": hour, "hours": (hour - run) // HOUR_MS})
                run = None
        elif run is None:
            run = hour
    if run is not None:
        gaps.append({"start": run, "end": closed, "hours": (closed - run) // HOUR_MS})
    return sorted(gaps, key=lambda g: -g["start"])[:10]


def status(ctx: Context, params: dict) -> dict:
    disk = _disk(ctx)
    batteries = _batteries(ctx)
    devices: dict[str, dict] = {}
    for attr, items in ctx.index.items():
        for s in items:
            latest = ctx.latest(s)
            if latest is None:
                continue
            d = devices.setdefault(s.device_id, {"id": s.device_id, "name": s.label, "room": s.room,
                                                 "last_ms": 0, "attributes": []})
            d["last_ms"] = max(d["last_ms"], latest[0])
            d["attributes"].append(attr)
    battery_of = {b["device_id"]: b["level"] for b in batteries}
    for d in devices.values():
        d["battery"] = battery_of.get(d["id"])
        d["attributes"].sort()
    return {
        "mode": ctx.meta("mode"),
        "auth_state": ctx.meta("auth_state"),
        "last_ok_ms": ctx.meta_int("last_ok_ms"),
        "last_error": ctx.meta("last_error"),
        "last_error_ms": ctx.meta_int("last_error_ms"),
        "history_fail_since_ms": ctx.meta_int("history_fail_since_ms"),
        "history_failures": ctx.meta_int("history_failures"),
        "status_fallback_ms": ctx.meta_int("status_fallback_ms"),
        "rollup_finished_ms": ctx.meta_int("rollup_finished_ms"),
        "closed_until": ctx.closed_until,
        "disk": disk,
        "devices": sorted(devices.values(), key=lambda d: (d["room"] or "", d["name"])),
        "gaps": _gaps(ctx),
        "alerts": alerts(ctx, disk, batteries),
    }


# ---- /api/system: RPi 상태 ----------------------------------------------------
def _sysmon_current(ctx: Context) -> dict | None:
    """모니터가 tmpfs 에 쓴 현재값. 없거나 깨졌으면 None."""
    if ctx.sysmon_current is None:
        return None
    try:
        data = json.loads(Path(ctx.sysmon_current).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _sysmon_rows(ctx: Context, start: int) -> tuple[list[tuple], list[tuple], bool]:
    """(1분 요약, 이벤트, DB 있음). 모니터를 켠 적이 없으면 빈 결과다."""
    path = ctx.sysmon_db
    if path is None or not Path(path).exists():
        return [], [], False
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        # cpu_avg, cpu_cores 는 모니터가 새 판으로 재시작해야 생긴다. 웹은 읽기 전용이라 없으면 NULL 로 읽는다.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(minutes)")}
        extra = ", ".join(c if c in cols else "NULL" for c in ("cpu_avg", "cpu_cores"))
        minutes = conn.execute(
            "SELECT ts_ms, temp_avg, temp_min, temp_max, freq_avg, load_avg, undervolt_s, overheat_s, "
            f"{extra} FROM minutes WHERE ts_ms>=? ORDER BY ts_ms", (start,),
        ).fetchall()
        events = conn.execute(
            "SELECT id, kind, start_ms, end_ms, seconds, interrupted, temp_c, freq_mhz, load1, procs_json "
            "FROM events ORDER BY start_ms DESC, id DESC LIMIT ?", (SYSMON_EVENT_LIMIT,),
        ).fetchall()
    except sqlite3.OperationalError:  # 모니터가 표를 만들기 전
        return [], [], False
    finally:
        conn.close()
    return minutes, events, True


def _cpu_count() -> int:
    """load 를 % 로 바꿀 때 나눌 코어 수.

    Linux 의 os.cpu_count() 는 켜져 있는 코어 수(_SC_NPROCESSORS_ONLN)라서
    maxcpus=1 로 부팅한 RPi 에서는 1 이 된다. 알 수 없으면 1 로 둔다.
    """
    return os.cpu_count() or 1


def _cores(raw) -> dict[str, float]:
    """minutes.cpu_cores JSON 을 {코어 번호: 사용률}로. 깨졌거나 없으면 빈 dict."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): float(v) for k, v in data.items()
            if str(k).isdigit() and isinstance(v, (int, float)) and not isinstance(v, bool)}


def _mean(values: list):
    return sum(values) / len(values) if values else None


def system(ctx: Context, params: dict) -> dict:
    now = ctx.now_ms
    period = _choice(params, "period", SYSMON_PERIODS, "24h")
    bucket_ms, bucket_count = SYSMON_PERIODS[period]
    # 1시간 보기는 끝난 분까지만 쓴다. 진행 중인 분은 아직 1분 요약이 없어 끝이 늘 비기 때문이다.
    end = now - now % bucket_ms + (bucket_ms if period == "24h" else 0)
    start = end - bucket_count * bucket_ms
    current = _sysmon_current(ctx)
    minutes, events, has_db = _sysmon_rows(ctx, start)

    by_minute = {r[0]: r for r in minutes}
    for p in (current or {}).get("pending") or []:  # 아직 DB에 저장되지 않은 최근 분
        if isinstance(p, dict) and p.get("ts_ms", 0) >= start:
            by_minute[p["ts_ms"]] = (p["ts_ms"], p.get("temp_avg"), p.get("temp_min"), p.get("temp_max"),
                                     p.get("freq_avg"), p.get("load_avg"), p.get("undervolt_s", 0), p.get("overheat_s", 0),
                                     p.get("cpu_avg"), p.get("cpu_cores"))

    groups: list[list[tuple]] = [[] for _ in range(bucket_count)]
    for ts, row in by_minute.items():
        i = (ts - start) // bucket_ms
        if 0 <= i < bucket_count:
            groups[i].append(row)

    def column(index, fn, digits):
        out = []
        for rows in groups:
            values = [r[index] for r in rows if r[index] is not None]
            out.append(_r(fn(values), digits) if values else None)
        return out

    # 코어별: {"0": [구간별 평균 또는 null, ...], "1": [...]}. 코어 번호는 숫자 순서
    core_values: dict[str, list[list[float]]] = {}
    for i, rows in enumerate(groups):
        for r in rows:
            for core, pct in _cores(r[9] if len(r) > 9 else None).items():
                core_values.setdefault(core, [[] for _ in range(bucket_count)])[i].append(pct)
    cpu_cores = {core: [_r(_mean(v), 1) if v else None for v in core_values[core]]
                 for core in sorted(core_values, key=int)}

    per_bucket_minutes = bucket_ms // 60000
    series = {
        "buckets": [start + i * bucket_ms for i in range(bucket_count)],
        "temp_avg": column(1, _mean, 1), "temp_min": column(2, min, 1), "temp_max": column(3, max, 1),
        "freq": column(4, _mean, 0), "load": column(5, _mean, 2),
        "cpu": [_r(_mean(v), 1) if (v := [r[8] for r in rows if len(r) > 8 and r[8] is not None]) else None
                for rows in groups],
        "cpu_cores": cpu_cores,
        "undervolt_s": [sum(r[6] or 0 for r in rows) for rows in groups],
        "overheat_s": [sum(r[7] or 0 for r in rows) for rows in groups],
        "coverage": [_r(min(1.0, len(rows) / per_bucket_minutes), 2) if rows else None for rows in groups],
    }

    out_events = []
    for r in events:
        try:
            procs = json.loads(r[9]) if r[9] else []
        except ValueError:
            procs = []
        out_events.append({"id": r[0], "kind": r[1], "start": r[2], "end": r[3], "seconds": r[4],
                           "interrupted": bool(r[5]), "temp_c": _r(r[6], 1), "freq_mhz": _r(r[7], 0),
                           "load1": _r(r[8], 2), "procs": procs})

    view = None
    if current is not None:
        ts = int(current.get("ts_ms", 0))
        view = {k: current.get(k) for k in ("ts_ms", "temp_c", "freq_mhz", "load1", "cpu_pct", "cpu_pct_1m", "cpu_cores", "mem_total_mb", "mem_avail_mb",
                                            "uptime_s", "undervoltage", "disk_free_gb", "disk_total_gb", "capabilities")}
        view["active"] = current.get("active") or []
        view["top"] = current.get("top") or []
        view["stale"] = now - ts > SYSMON_STALE_MS
    return {"available": has_db or current is not None, "cpu_count": _cpu_count(), "current": view,
            "series": series, "events": out_events}


def meta(ctx: Context, params: dict) -> dict:
    rooms = ctx.rooms()

    def listing(items):
        return [{"id": s.device_id, "name": s.place, "color": ctx.color(s.place, rooms)} for s in items]

    return {
        "now": ctx.now_ms,
        "version": __version__,
        "closed_until": ctx.closed_until,
        "rooms": rooms,
        "sensors": {
            "temperature": listing(ctx.index.get("temperature", [])),
            "humidity": listing(ctx.index.get("humidity", [])),
            "illuminance": listing(ctx.index.get("illuminance", [])),
            "occupancy": listing(ctx.occupancy_series()),
        },
        "bins": LUX_LABELS,
        "alerts": alerts(ctx),
    }



# ---- /hub/status: SmartThings 허브용 요약 -----------------------------------------
# 허브의 Edge 드라이버가 60초마다 읽는다. 로그인 없이 허용 주소에서만 열리므로
# 기기·방 이름, 오류 문구, 비밀값을 넣지 않는다. 새로 측정하지 않고 저장된 최근 값만 읽는다.
# 수집 판정 (contact 이상 = open 은 ok=false 일 때):
#   reauth   SmartThings 재인증 필요                     -> 이상
#   unknown  수집이 한 번도 성공한 적 없음               -> 이상
#   stopped  마지막 수집 성공이 30분(STALE_COLLECT_MS) 넘음 -> 이상
#   history  이력 조회가 10분 넘게 연속 실패 (현재값 임시 수집 중일 수 있음)
#            -> 60분(HUB_HISTORY_ALERT_MS)을 넘기면 이상, 그 전에는 정상으로 둔다
#   ok       그 밖
HUB_HISTORY_ALERT_MS = 60 * 60 * 1000
HUB_STATE_TEXT = {
    "ok": "정상", "history": "이력 지연", "stopped": "수집 멈춤", "reauth": "재인증 필요", "unknown": "수집 기록 없음",
}


def _kst_text(ms: int | None) -> str | None:
    if ms is None:
        return None
    t = time.gmtime((ms + KST_OFFSET_MS) // 1000)
    return f"{t.tm_mon:02d}-{t.tm_mday:02d} {t.tm_hour:02d}:{t.tm_min:02d}"


def collect_health(ctx: Context) -> tuple[str, bool, int | None]:
    """(상태, 정상 여부, 지속 분). 판정 기준은 위 주석."""
    now = ctx.now_ms
    if ctx.meta("auth_state") == "reauth_required":
        return "reauth", False, None
    last_ok = ctx.meta_int("last_ok_ms")
    if last_ok is None:
        return "unknown", False, None
    if now - last_ok > STALE_COLLECT_MS:
        return "stopped", False, (now - last_ok) // 60000
    fail_since = ctx.meta_int("history_fail_since_ms")
    if fail_since is not None and now - fail_since > STALE_HISTORY_MS:
        return "history", now - fail_since <= HUB_HISTORY_ALERT_MS, (now - fail_since) // 60000
    return "ok", True, None


def hub_status(ctx: Context) -> dict:
    state, ok, minutes = collect_health(ctx)
    text = HUB_STATE_TEXT[state] + (f" {minutes}분" if minutes is not None else "")
    last_ok = ctx.meta_int("last_ok_ms")
    current = _sysmon_current(ctx)
    fresh = current is not None and ctx.now_ms - int(current.get("ts_ms", 0) or 0) <= SYSMON_STALE_MS

    def num(key, digits):
        value = current.get(key) if fresh else None
        return _r(float(value), digits) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    try:
        disk = shutil.disk_usage(ctx.db_path.parent)
        disk_pct = _r(disk.used / disk.total * 100, 1) if disk.total else None
    except OSError:
        disk_pct = None
    return {
        "now_ms": ctx.now_ms,
        "collect_ok": ok,
        "collect_state": state,
        "collect_text": text,
        "last_ok_ms": last_ok,
        "last_ok_text": _kst_text(last_ok),
        "cpu_temp_c": num("temp_c", 1),
        "cpu_mhz": num("freq_mhz", 0),
        # 최근 1분 평균. 이전 판 모니터의 현재값 파일에는 cpu_pct_1m 이 없어 순간값으로 대신한다.
        "cpu_pct": num("cpu_pct_1m", 1) if num("cpu_pct_1m", 1) is not None else num("cpu_pct", 1),
        "disk_used_pct": disk_pct,
        "sysmon_fresh": fresh,
    }

# 집계가 바뀌어야 결과가 바뀌는 API (rollup 완료 시각으로 캐시)
ROLLUP_ROUTES = {"/api/series": series, "/api/lux-dist": lux_dist, "/api/occupancy": occupancy, "/api/plugs": plugs}
# 현재값을 포함하는 API (짧게만 캐시)
LIVE_ROUTES = {"/api/summary": summary, "/api/status": status, "/api/meta": meta, "/api/system": system}
