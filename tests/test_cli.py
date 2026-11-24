"""Tests for CLI configuration, validation, and systemd generation."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from telegram_agy.cli import main
from telegram_agy.config import load_access_config, load_bot_token


class CliTests(unittest.TestCase):
    def test_configure_token_stdin_and_access_validation(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            state_dir = Path(tmpdir) / "state"
            ws = Path(tmpdir) / "ws"
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
            cfg = load_access_config(state_dir)
            self.assertEqual(cfg["execution"]["approvalTimeoutSeconds"], 90)
            self.assertEqual(cfg["textChunkLimit"], 2000)
            self.assertEqual(cfg["execution"]["mode"], "plan")

            # Invalid settings rejected
            with patch("sys.stderr", io.StringIO()):
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

            # systemd includes WorkingDirectory and --cwd
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main(["--state-dir", str(state_dir), "systemd", "--cwd", str(ws)])
            self.assertEqual(rc, 0)
            unit = buf.getvalue()
            self.assertIn(f"WorkingDirectory={ws.resolve()}", unit)
            self.assertIn(f"--cwd {ws.resolve()}", unit)


if __name__ == "__main__":
    unittest.main()
