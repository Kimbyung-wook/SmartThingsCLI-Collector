#!/usr/bin/env python3
"""개발·성능 측정용 가짜 데이터 생성기. 실제 RPi의 DB에 절대 쓰지 마세요.

    python tools/generate_fake_data.py --db <경로> [--days 400] [--seed 7]

온습도 4, 조도 4, 재실 2, 플러그 10기(그중 6기는 전력 측정)를 흉내 낸다.
센서처럼 값이 일정 이상 변하거나 일정 시간이 지났을 때만 보고한다.
수집 중단 구간도 넣어서 coverage 표시를 확인할 수 있다.
DB 옆 sysmon/ 폴더에 시스템 모니터의 가짜 기록(sysmon.db, current.json)도 만든다.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stcollector.db import Database  # noqa: E402
from stcollector.settings import sysmon_db_path  # noqa: E402
from stcollector.sysmon import Sample, SysmonStore, write_runtime  # noqa: E402

MIN = 60_000
HOUR = 60 * MIN
DAY = 24 * HOUR
KST = 9 * HOUR

ROOMS = [
    {"id": "living", "name": "거실", "t": 23.6, "h": 47, "sun": 1150, "lamp": 210},
    {"id": "bed", "name": "안방", "t": 22.2, "h": 52, "sun": 420, "lamp": 90},
    {"id": "small", "name": "작은방", "t": 21.3, "h": 55, "sun": 260, "lamp": 130},
    {"id": "study", "name": "서재", "t": 22.8, "h": 44, "sun": 640, "lamp": 320},
]
PLUGS = [  # (이름, 방, 전력 W(0=측정 안 됨), 켜지는 시각, 꺼지는 시각, 사용 확률)
    ("거실 보조등", "living", 9, 18, 23, 0.9),
    ("안방 보조등", "bed", 7, 21, 24, 0.8),
    ("공기청정기", "living", 28, 8, 22, 0.8),
    ("가습기", "bed", 35, 22, 7, 0.4),
    ("TV", "living", 95, 19, 23, 0.7),
    ("전기요", "bed", 0, 22, 6, 0.4),
    ("서재 스탠드", "study", 0, 20, 23, 0.6),
    ("정수기", "small", 45, 0, 24, 1.0),
    ("제습기", "small", 0, 12, 18, 0.2),
    ("충전 스테이션", "study", 0, 21, 8, 0.7),
]


def device_id(kind: str, i: int) -> str:
    return f"{kind}-0000-4000-8000-{i:012d}"


def outdoor(ms: int) -> float:
    day = (ms + KST) // DAY
    return 13 + 13 * math.sin(2 * math.pi * (day % 365 - 105) / 365)


def hour_of_day(ms: int) -> float:
    return ((ms + KST) % DAY) / HOUR


def weekday(ms: int) -> int:
    return (((ms + KST) // DAY) + 3) % 7


def occupied_probability(room: str, hod: float, wd: int) -> float:
    if room == "living":
        if wd >= 5:
            return 0.75 if 10 <= hod < 23 else 0.04
        return 0.85 if 18.5 <= hod < 23 else 0.5 if 7 <= hod < 8.5 else 0.04
    return 0.7 if wd < 5 and hod >= 20 else 0.55 if wd >= 5 and 13 <= hod < 18 else 0.06


class Reporter:
    """값이 임계값 이상 변하거나 최대 간격이 지나면 보고한다."""

    def __init__(self, threshold, max_interval_ms, relative=False):
        self.threshold, self.max_interval_ms, self.relative = threshold, max_interval_ms, relative
        self.last_value = None
        self.last_ts = 0

    def should_report(self, ts, value) -> bool:
        if self.last_value is None or ts - self.last_ts >= self.max_interval_ms:
            return True
        delta = abs(value - self.last_value)
        limit = self.threshold * max(self.last_value, 1) if self.relative else self.threshold
        return delta >= limit

    def mark(self, ts, value):
        self.last_ts, self.last_value = ts, value


def devices():
    out = []
    for i, r in enumerate(ROOMS):
        out.append({"device_id": device_id("clim", i), "label": f"{r['name']} 온습도", "name": "온습도 센서",
                    "room_id": r["id"], "room_name": r["name"], "location_id": "fake",
                    "capabilities": {"temperatureMeasurement", "relativeHumidityMeasurement", "battery"}})
        out.append({"device_id": device_id("luxx", i), "label": f"{r['name']} 조도", "name": "조도 센서",
                    "room_id": r["id"], "room_name": r["name"], "location_id": "fake",
                    "capabilities": {"illuminanceMeasurement", "battery"}})
    for i, room in enumerate(("living", "study")):
        name = next(r["name"] for r in ROOMS if r["id"] == room)
        out.append({"device_id": device_id("occp", i), "label": f"{name} 재실", "name": "재실 센서",
                    "room_id": room, "room_name": name, "location_id": "fake",
                    "capabilities": {"motionSensor", "battery"}})
    for i, (label, room, watt, *_rest) in enumerate(PLUGS):
        name = next(r["name"] for r in ROOMS if r["id"] == room)
        caps = {"switch"} | ({"powerMeter", "energyMeter"} if watt else set())
        out.append({"device_id": device_id("plug", i), "label": label, "name": "스마트플러그",
                    "room_id": room, "room_name": name, "location_id": "fake", "capabilities": caps})
    return out


def row(dev, cap, attr, ts, num=None, text=None, unit=None):
    return (dev, "main", cap, attr, ts, num, text, unit, "history", None)


def generate(db: Database, days: int, seed: int, now_ms: int) -> int:
    rng = random.Random(seed)
    step = 5 * MIN
    start = (now_ms - days * DAY) // (6 * HOUR) * (6 * HOUR)  # 보고 주기 계산이 맞도록 정렬
    # 24시간 보기에서 짧은 공백(4시간, 점선으로 이어짐)과 긴 공백(10시간, 끊김)이 함께 보이게 한다
    outages = [(now_ms - 120 * DAY, now_ms - 117 * DAY), (now_ms - 5 * DAY - 6 * HOUR, now_ms - 5 * DAY),
               (now_ms - 24 * HOUR, now_ms - 20 * HOUR), (now_ms - 16 * HOUR, now_ms - 6 * HOUR)]
    in_outage = lambda t: any(a <= t < b for a, b in outages)  # noqa: E731

    db.upsert_devices(devices())
    climate = [(Reporter(0.2, 45 * MIN), Reporter(1.5, 45 * MIN)) for _ in ROOMS]
    light = [Reporter(0.2, 30 * MIN, relative=True) for _ in ROOMS]
    occ = [{"state": False, "next": start} for _ in range(2)]
    plug_state = [False] * len(PLUGS)
    energy = [rng.uniform(5, 40) for _ in PLUGS]
    power_rep = [Reporter(3, 30 * MIN) for _ in PLUGS]
    cloud = 1.0
    drift = [0.0, 0.0]
    buf = []
    total = 0
    t = start
    today = None
    lamp_on = [False] * 4

    while t < now_ms:
        day = (t + KST) // DAY
        if day != today:
            today = day
            cloud = 0.25 + 0.75 * rng.random()
        hod, wd = hour_of_day(t), weekday(t)
        drift[0] = max(-1, min(1, drift[0] + rng.gauss(0, 0.04)))
        drift[1] = max(-5, min(5, drift[1] + rng.gauss(0, 0.25)))
        out = outdoor(t)
        down = in_outage(t)

        for i, r in enumerate(ROOMS):
            jitter = rng.randint(0, 120_000)
            temp = round(r["t"] + 0.07 * (out - 13) + 0.8 * math.sin(2 * math.pi * (hod - 10) / 24) + drift[0], 1)
            hum = round(max(22, min(88, r["h"] + 0.75 * (out - 13) - 2.2 * math.sin(2 * math.pi * (hod - 10) / 24)
                                    + drift[1])), 0)
            tr, hr = climate[i]
            if not down:
                if tr.should_report(t, temp):
                    buf.append(row(device_id("clim", i), "temperatureMeasurement", "temperature", t + jitter, temp, unit="C"))
                    tr.mark(t, temp)
                if hr.should_report(t, hum):
                    buf.append(row(device_id("clim", i), "relativeHumidityMeasurement", "humidity", t + jitter + 7, hum, unit="%"))
                    hr.mark(t, hum)
            daylight = math.sin(math.pi * (hod - 6.8) / 11.4) ** 1.6 if 6.8 < hod < 18.2 else 0
            sun = r["sun"] * daylight * cloud * (1 + 0.15 * rng.gauss(0, 1))
            if rng.random() < 0.03:
                evening = hod >= 17.5 or hod < 1
                lamp_on[i] = evening and rng.random() < 0.8
            if hod >= 1 and hod < 17:
                lamp_on[i] = False
            lamp = r["lamp"] * (0.9 + 0.1 * rng.random()) if lamp_on[i] else 0
            lux = max(0.0, round(sun + lamp + rng.uniform(0, 0.4), 1))
            if not down and light[i].should_report(t, lux):
                buf.append(row(device_id("luxx", i), "illuminanceMeasurement", "illuminance", t + jitter + 13, lux, unit="lux"))
                light[i].mark(t, lux)

        for i, room in enumerate(("living", "study")):
            o = occ[i]
            if t >= o["next"]:
                want = rng.random() < occupied_probability(room, hod, wd)
                if want != o["state"]:
                    o["state"] = want
                    if not down:
                        buf.append(row(device_id("occp", i), "motionSensor", "motion", t + rng.randint(0, 60_000),
                                       text="active" if want else "inactive"))
                o["next"] = t + rng.randint(10, 40) * MIN

        for i, (label, _room, watt, on_h, off_h, prob) in enumerate(PLUGS):
            active = (on_h <= hod < off_h) if on_h < off_h else (hod >= on_h or hod < off_h)
            desired = active and (((day * 7919 + i * 104729) % 1000) / 1000 < prob)
            if desired != plug_state[i]:
                plug_state[i] = desired
                if not down:
                    buf.append(row(device_id("plug", i), "switch", "switch", t + 31, text="on" if desired else "off"))
            if watt:
                draw = watt * (0.9 + 0.2 * rng.random()) if plug_state[i] else 0.3
                energy[i] += draw * step / HOUR / 1000
                if not down and power_rep[i].should_report(t, draw):
                    buf.append(row(device_id("plug", i), "powerMeter", "power", t + 37, round(draw, 1), unit="W"))
                    power_rep[i].mark(t, draw)
                if not down and t % (30 * MIN) == 0:
                    buf.append(row(device_id("plug", i), "energyMeter", "energy", t + 41, round(energy[i], 3), unit="kWh"))

        if t % (6 * HOUR) == 0:
            for kind, n, base in (("clim", 4, 92), ("luxx", 4, 80), ("occp", 2, 70)):
                for i in range(n):
                    level = base - i * 12 - (t - start) / DAY * 0.08
                    if kind == "clim" and i == 3:
                        level = 18
                    buf.append(row(device_id(kind, i), "battery", "battery", t + 3, max(1, round(level)), unit="%"))

        if len(buf) >= 20_000:
            total += db.insert_readings(buf)
            buf.clear()
        t += step

    total += db.insert_readings(buf)
    db.set_meta("mode", "history")
    db.set_meta("auth_state", "ok")
    db.set_meta("last_ok_ms", now_ms - 2 * MIN)
    return total


def generate_sysmon(db_path: Path, seed: int, now_ms: int, days: int = 14) -> int:
    """RPi 상태의 가짜 기록. 프로세스 이름은 일반적인 값만 쓴다."""
    rng = random.Random(seed + 1)
    path = sysmon_db_path(db_path)
    store = SysmonStore(path)
    minute_now = now_ms - now_ms % MIN
    start = minute_now - days * DAY
    # (시작까지 남은 분, 지속 분): 전압 부족 구간
    undervolt = [(95, 3), (410, 1), (760, 2), (2600, 4), (9000, 2)]
    uv_minutes = {minute_now - (ago - i) * MIN for ago, length in undervolt for i in range(length)}
    rows = []
    for ts in range(start, minute_now, MIN):
        hod = hour_of_day(ts)
        collect = ts % (5 * MIN) < MIN
        rollup = int((ts / MIN) % 60) == 2
        temp = 47 + 4 * math.sin(2 * math.pi * (hod - 15) / 24) + (5 if collect else 0) + (8 if rollup else 0) + rng.gauss(0, 0.5)
        load = max(0.02, 0.15 + (0.7 if collect else 0) + (1.1 if rollup else 0) + rng.gauss(0, 0.05))
        uv = 25 if ts in uv_minutes else 0
        rows.append({
            "ts_ms": ts, "temp_avg": round(temp, 1), "temp_min": round(temp - 0.6, 1), "temp_max": round(temp + 1.1, 1),
            "freq_avg": 900.0 if collect or rollup else 700.0 + rng.randint(0, 50), "load_avg": round(load, 2),
            "mem_avail_mb": 620 - (130 if collect else 0), "disk_free_gb": 24.8,
            "undervolt_s": uv, "overheat_s": 0, "throttle_s": 0, "samples": 12,
        })
    store.add_minutes(rows)
    procs = [
        {"name": "smartthings", "cpu_s": 31.4, "pct": 52.3},
        {"name": "python3 (수집)", "cpu_s": 4.2, "pct": 7.0},
        {"name": "python3 (모니터)", "cpu_s": 0.6, "pct": 1.0},
        {"name": "sshd", "cpu_s": 0.2, "pct": 0.3},
    ]
    rollup_procs = [{"name": "python3 (집계)", "cpu_s": 22.5, "pct": 37.5}, *procs[1:]]
    for i, (ago, length) in enumerate(undervolt):
        at = minute_now - ago * MIN + 7000
        sample = Sample(ts_ms=at, temp_c=54.0 + i, freq_mhz=900.0, load1=1.2 + i / 10)
        store.end_event(store.start_event("undervoltage", at, sample, rollup_procs if i == 1 else procs), at + (length * 25 + 3) * 1000, length * 25)
    hot = minute_now - 300 * MIN
    store.end_event(store.start_event("overheat", hot, Sample(ts_ms=hot, temp_c=80.4, freq_mhz=900.0, load1=2.4), rollup_procs), hot + 45_000, 45)
    cut = minute_now - 1700 * MIN
    store.start_event("undervoltage", cut, Sample(ts_ms=cut, temp_c=58.1, freq_mhz=900.0, load1=1.9), procs)
    store.close_open_events()
    boot = minute_now - 1695 * MIN
    store.end_event(store.start_event("boot", boot, None, []), boot, 0)
    store.close()
    write_runtime(path.parent / "current.json", {
        "ts_ms": now_ms, "temp_c": 51.2, "freq_mhz": 900.0, "load1": 0.42, "mem_total_mb": 905, "mem_avail_mb": 588,
        "uptime_s": 1695 * 60, "undervoltage": False, "freq_capped": None, "throttled": None, "soft_temp": None,
        "active": [], "top": procs[:3], "disk_free_gb": 24.8, "disk_total_gb": 29.0,
        "capabilities": {"undervoltage": True, "throttle_bits": False}, "pending": [],
    })
    return len(rows)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True)
    p.add_argument("--days", type=int, default=400)
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()
    path = Path(args.db)
    if str(path).startswith("/var/lib/stcollector"):
        print("실제 수집 DB에는 가짜 데이터를 넣지 않습니다.", file=sys.stderr)
        return 2
    db = Database(path)
    begin = time.time()
    now_ms = int(time.time() * 1000)
    n = generate(db, args.days, args.seed, now_ms)
    db.close()
    generate_sysmon(path, args.seed, now_ms)
    print(f"측정값 {n:,}건 생성, {time.time() - begin:.1f}초, 파일 {path.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
