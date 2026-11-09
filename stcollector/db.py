"""SQLite 저장소."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

SCHEMA_VERSION = "2"
_SCHEMA = (Path(__file__).with_name("schema.sql")).read_text("utf-8")

# (device_id, component, capability, attribute, ts_ms, value_num, value_text, unit, source, event_hash)
Reading = tuple


def _now_ms() -> int:
    return int(time.time() * 1000)


class Database:
    def __init__(self, path: Path | str):
        path = Path(path) if str(path) != ":memory:" else path
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)
        self.set_meta("schema_version", SCHEMA_VERSION)

    def close(self) -> None:
        self.conn.close()

    # ---- meta -------------------------------------------------------------
    def set_meta(self, key: str, value: object) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, None if value is None else str(value)),
            )

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    # ---- devices ----------------------------------------------------------
    def upsert_devices(self, devices: Iterable[dict]) -> None:
        now = _now_ms()
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO devices(device_id, label, name, room_id, room_name, location_id,
                                    capabilities_json, updated_at_ms)
                VALUES(:device_id, :label, :name, :room_id, :room_name, :location_id,
                       :capabilities_json, :updated_at_ms)
                ON CONFLICT(device_id) DO UPDATE SET
                    label = excluded.label, name = excluded.name,
                    room_id = excluded.room_id, room_name = excluded.room_name,
                    location_id = excluded.location_id,
                    capabilities_json = excluded.capabilities_json,
                    updated_at_ms = excluded.updated_at_ms
                """,
                [
                    {**d, "capabilities_json": json.dumps(sorted(d["capabilities"])), "updated_at_ms": now}
                    for d in devices
                ],
            )

    def device_labels(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT device_id, label, room_name FROM devices").fetchall()
        return {
            r["device_id"]: f"{r['label']}" + (f" ({r['room_name']})" if r["room_name"] else "")
            for r in rows
        }

    # ---- readings ---------------------------------------------------------
    def insert_readings(self, rows: Iterable[Reading]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        # total_changes 는 집계 트리거의 변경까지 세므로 rowcount 를 쓴다.
        with self.conn:
            cur = self.conn.executemany(
                "INSERT OR IGNORE INTO readings(device_id, component, capability, attribute, ts_ms, "
                "value_num, value_text, unit, source, event_hash) VALUES(?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return max(cur.rowcount, 0)

    # ---- cursors ----------------------------------------------------------
    def get_cursor(self, device_id: str) -> int | None:
        row = self.conn.execute(
            "SELECT last_epoch_ms FROM cursors WHERE device_id = ?", (device_id,)
        ).fetchone()
        return row["last_epoch_ms"] if row else None

    def set_cursor(self, device_id: str, epoch_ms: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO cursors(device_id, last_epoch_ms, updated_at_ms) VALUES(?, ?, ?) "
                "ON CONFLICT(device_id) DO UPDATE SET last_epoch_ms = excluded.last_epoch_ms, "
                "updated_at_ms = excluded.updated_at_ms",
                (device_id, int(epoch_ms), _now_ms()),
            )
