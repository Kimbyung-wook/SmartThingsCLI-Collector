"""센서별 실제 보고 간격 리포트 (PLAN §6.1, M1 산출물)."""

from __future__ import annotations

import statistics
from itertools import groupby

from .db import Database


def interval_report(db: Database, days: int, now_ms: int) -> list[dict]:
    since = now_ms - days * 86_400_000
    cur = db.conn.execute(
        "SELECT device_id, capability, attribute, ts_ms FROM readings "
        "WHERE ts_ms >= ? ORDER BY device_id, capability, attribute, ts_ms",
        (since,),
    )
    labels = db.device_labels()
    report = []
    for (device_id, cap, attr), group in groupby(cur, key=lambda r: (r[0], r[1], r[2])):
        ts = [r[3] for r in group]
        gaps = [(b - a) / 1000 for a, b in zip(ts, ts[1:]) if b > a]
        entry = {
            "device": labels.get(device_id, device_id),
            "capability": cap,
            "attribute": attr,
            "count": len(ts),
            "median_s": None,
            "p90_s": None,
            "max_s": None,
        }
        if gaps:
            entry["median_s"] = statistics.median(gaps)
            entry["p90_s"] = statistics.quantiles(gaps, n=10)[8] if len(gaps) >= 2 else gaps[0]
            entry["max_s"] = max(gaps)
        report.append(entry)
    return report


def _fmt(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 120:
        return f"{seconds:.0f}s"
    if seconds < 7200:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def format_report(report: list[dict], days: int) -> str:
    lines = [
        f"최근 {days}일 보고 간격 (센서가 '값 변화'만 보고하면 변화 빈도에 해당합니다)",
        "",
        f"{'기기':<28} {'속성':<28} {'건수':>7} {'중앙값':>8} {'p90':>8} {'최대':>8}",
        "-" * 92,
    ]
    for e in report:
        lines.append(
            f"{e['device'][:28]:<28} {(e['capability'] + '.' + e['attribute'])[:28]:<28} "
            f"{e['count']:>7} {_fmt(e['median_s']):>8} {_fmt(e['p90_s']):>8} {_fmt(e['max_s']):>8}"
        )
    if not report:
        lines.append("(데이터 없음)")
    return "\n".join(lines)
