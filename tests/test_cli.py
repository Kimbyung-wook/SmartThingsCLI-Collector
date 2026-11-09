"""명령행: 집계, 웹서버 시작 거부, 비밀번호 해시."""

import contextlib
import io
import unittest
from unittest import mock

from stcollector import __version__
from stcollector.__main__ import main
from stcollector.db import Database
from stcollector.web.session import valid_hash
from tests.helpers import TempDirMixin

CONFIG = """
[paths]
db = "{db}"
token = "{token}"
"""


class CliTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.db = self.tmp / "s.db"
        self.config = self.tmp / "config.toml"
        self.config.write_text(
            CONFIG.format(db=self.db.as_posix(), token=(self.tmp / "t.json").as_posix()), "utf-8"
        )

    def run_cli(self, *args):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = main(["--config", str(self.config), *args])
        return code, out.getvalue(), err.getvalue()

    def test_version_flag(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"stcollector {__version__}")

    def test_rollup_on_empty_db(self):
        code, _, _ = self.run_cli("rollup")
        self.assertEqual(code, 0)
        db = Database(self.db)
        self.assertIsNotNone(db.get_meta("rollup_closed_until_ms"))
        db.close()

    def test_web_refuses_without_password(self):
        code, _, err = self.run_cli("web")
        self.assertEqual(code, 2)
        self.assertIn("비밀번호", err)

    def test_hash_password_output(self):
        with mock.patch("getpass.getpass", return_value="long-enough-pass"):
            code, out, _ = self.run_cli("hash-password")
        self.assertEqual(code, 0)
        line = next(x for x in out.splitlines() if x.startswith("password_hash"))
        self.assertTrue(valid_hash(line.split('"')[1]))
        self.assertNotIn("long-enough-pass", out)

    def test_hash_password_rejects_short(self):
        with mock.patch("getpass.getpass", return_value="short"):
            self.assertEqual(self.run_cli("hash-password")[0], 1)


if __name__ == "__main__":
    unittest.main()
