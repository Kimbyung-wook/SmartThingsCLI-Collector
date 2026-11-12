"""Edge 드라이버 파일끼리, 그리고 /hub/status 응답과 이름이 맞는지 확인한다 (Lua 실행은 하지 않는다)."""

import json
import re
import types
import unittest
from pathlib import Path

from stcollector.web import api

ROOT = Path(__file__).resolve().parent.parent / "edge-driver"
NS = "angelconcert61717"


def camel(name: str) -> str:
    """capabilities:create 가 name 에서 id 를 만드는 방식 (사용자 시험: "CPU Clock Test" -> cpuClockTest)."""
    words = name.split()
    return words[0].lower() + "".join(w[:1].upper() + w[1:].lower() for w in words[1:])


class EdgeDriverFilesTests(unittest.TestCase):
    def setUp(self):
        self.lua = (ROOT / "src" / "init.lua").read_text("utf-8")
        self.profile = (ROOT / "profiles" / "stcollector-rpi.yml").read_text("utf-8")
        self.caps = {}
        for path in sorted((ROOT / "capabilities").glob("*.json")):
            if not path.name.endswith(".presentation.json"):
                self.caps[path] = json.loads(path.read_text("utf-8"))

    def test_camel_rule_matches_user_test(self):
        self.assertEqual(camel("CPU Clock Test"), "cpuClockTest")

    def test_capabilities_presentations_profile_and_lua_agree(self):
        self.assertEqual(len(self.caps), 5)
        for path, cap in self.caps.items():
            cap_id = camel(cap["name"])
            (attr,) = cap["attributes"]
            pres = json.loads(path.with_name(path.stem + ".presentation.json").read_text("utf-8"))
            self.assertEqual(pres["id"], f"{NS}.{cap_id}", path.name)
            self.assertEqual(pres["version"], 1)
            self.assertIn(f"{attr}.value", json.dumps(pres))
            self.assertIn(f"- id: {NS}.{cap_id}\n", self.profile)
            self.assertIn(f'capabilities[NS .. ".{cap_id}"]', self.lua)
            self.assertRegex(self.lua, rf"\.{attr}\(\{{ value = v")

    def test_profile_name_used_by_lua(self):
        name = re.search(r"^name: (\S+)$", self.profile, re.M).group(1)
        self.assertIn(f'PROFILE = "{name}"', self.lua)

    def test_lua_reads_existing_status_fields(self):
        used = set(re.findall(r"data\.(\w+)", self.lua))
        ctx_fields = {"now_ms", "collect_ok", "collect_state", "collect_text", "last_ok_ms", "last_ok_text",
                      "cpu_temp_c", "cpu_mhz", "load1", "disk_used_pct", "sysmon_fresh"}
        self.assertTrue(used, "init.lua 가 응답 필드를 읽지 않음")
        self.assertLessEqual(used, ctx_fields)
        # api.hub_status 의 키 목록과 위 목록이 같아야 한다
        ctx = types.SimpleNamespace(now_ms=0, meta=lambda key: None, meta_int=lambda key: None,
                                    sysmon_current=None, db_path=ROOT / "config.yml")
        self.assertEqual(set(api.hub_status(ctx)), ctx_fields)

    def test_driver_has_no_write_requests(self):
        self.assertNotRegex(self.lua, r'method\s*=\s*"(POST|PUT|DELETE|PATCH)"')
        self.assertIn('"/hub/status"', self.lua)


if __name__ == "__main__":
    unittest.main()
