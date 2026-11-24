"""Tests for CLI configuration, validation, PID safety, and systemd generation."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from telegram_agy.cli import _pid_matches_telegram_agy, main
from telegram_agy.config import DEFAULT_SAFE_TOOLS, load_access_config, load_bot_token


class CliTests(unittest.TestCase):
    def test_configure_token_stdin_and_access_validation(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            state_dir = Path(tmpdir) / "state"
            ws = Path(tmpdir) / "dir with spaces"
            ws.mkdir()

            with patch("sys.stdin", io.StringIO("12345:stdin_secret\n")), redirect_stdout(io.StringIO()):
                rc = main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "configure",
                        "--token-stdin",
                        "--no-verify",
                        "--cwd",
                        str(ws),
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertEqual(load_bot_token(state_dir), "12345:stdin_secret")

            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(["--state-dir", str(state_dir), "access", "set", "approvalTimeoutSeconds", "90"]),
                    0,
                )
                self.assertEqual(
                    main(["--state-dir", str(state_dir), "access", "set", "textChunkLimit", "2000"]),
                    0,
                )
                self.assertEqual(
                    main(["--state-dir", str(state_dir), "access", "set", "mode", "plan"]),
                    0,
                )
                self.assertEqual(
                    main(["--state-dir", str(state_dir), "access", "set", "safeTools", "read_file,list_dir"]),
                    0,
                )
                self.assertEqual(
                    main(["--state-dir", str(state_dir), "access", "set", "extraArgs", "--", "--foo,--bar"]),
                    0,
                )
            cfg = load_access_config(state_dir)
            self.assertEqual(cfg["execution"]["approvalTimeoutSeconds"], 90)
            self.assertEqual(cfg["textChunkLimit"], 2000)
            self.assertEqual(cfg["execution"]["mode"], "plan")
            self.assertEqual(cfg["execution"]["safeTools"], ["read_file", "list_dir"])
            self.assertEqual(cfg["execution"]["extraArgs"], ["--foo", "--bar"])

            # Empty safeTools resets to defaults
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(["--state-dir", str(state_dir), "access", "set", "safeTools", ""]),
                    0,
                )
            self.assertEqual(load_access_config(state_dir)["execution"]["safeTools"], DEFAULT_SAFE_TOOLS)

            # Invalid IDs and settings rejected cleanly without traceback
            with patch("sys.stderr", io.StringIO()):
                self.assertEqual(main(["--state-dir", str(state_dir), "access", "allow", "abc"]), 1)
                self.assertEqual(
                    main(["--state-dir", str(state_dir), "access", "group", "allow", "-1001", "abc"]),
                    1,
                )
                self.assertNotEqual(
                    main(["--state-dir", str(state_dir), "access", "set", "approvalTimeoutSeconds", "300"]),
                    0,
                )
                self.assertNotEqual(
                    main(["--state-dir", str(state_dir), "access", "set", "textChunkLimit", "9999"]),
                    0,
                )
                self.assertNotEqual(
                    main(["--state-dir", str(state_dir), "access", "set", "mode", "bogus"]),
                    0,
                )
                self.assertNotEqual(
                    main(["--state-dir", str(state_dir), "access", "set", "model", "--", "--bad"]),
                    0,
                )

            # systemd quotes paths with spaces
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main(["--state-dir", str(state_dir), "systemd", "--cwd", str(ws)])
            self.assertEqual(rc, 0)
            unit = buf.getvalue()
            self.assertIn(f'WorkingDirectory="{ws.resolve()}"', unit)
            self.assertIn(f'--cwd "{ws.resolve()}"', unit)

    def test_pid_matches_non_proc_fallback_fails_safe(self) -> None:
        with patch("telegram_agy.cli.Path.is_dir", return_value=False):
            with patch("telegram_agy.cli.subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = "python3 -m telegram_agy run"
                self.assertTrue(_pid_matches_telegram_agy(1234))

                mock_run.return_value.stdout = "/usr/bin/nginx"
                self.assertFalse(_pid_matches_telegram_agy(1234))


if __name__ == "__main__":
    unittest.main()
