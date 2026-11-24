"""Tests for agy_runner command construction, env sanitization, timeout, and cancel."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_agy.agy_runner import AgyRunner, build_agy_command, parse_stream_json_lines, resolve_agy_bin


class AgyRunnerTests(unittest.TestCase):
    def test_build_agy_command_modes_and_sandbox_independence(self) -> None:
        cmd_relay = build_agy_command(
            "hello",
            conversation_id="conv-1",
            execution_cfg={"permissionMode": "relay", "sandbox": True, "mode": "auto", "model": "gemini"},
        )
        self.assertIn("--sandbox", cmd_relay)
        self.assertNotIn("--dangerously-skip-permissions", cmd_relay)
        self.assertIn("--conversation=conv-1", cmd_relay)
        self.assertIn("--model=gemini", cmd_relay)
        self.assertIn("--mode=auto", cmd_relay)

        # sandbox=False works even in relay mode
        cmd_relay_nosandbox = build_agy_command(
            "hello",
            execution_cfg={"permissionMode": "relay", "sandbox": False},
        )
        self.assertNotIn("--sandbox", cmd_relay_nosandbox)

        # sandbox=True works even in full-auto mode
        cmd_full_sandbox = build_agy_command(
            "hello",
            execution_cfg={"permissionMode": "full-auto", "sandbox": True},
        )
        self.assertIn("--sandbox", cmd_full_sandbox)
        self.assertIn("--dangerously-skip-permissions", cmd_full_sandbox)

        # Flag injection in model rejected
        with self.assertRaises(ValueError):
            build_agy_command("hi", session_cfg={"model": "--evil"})

    def test_parse_stream_json_deltas_and_result(self) -> None:
        lines = [
            json.dumps({"event": "step_update", "step_update": {"text_delta": "Hello "}}),
            json.dumps({"event": "step_update", "step_update": {"text_delta": "world!"}}),
            json.dumps({"event": "result", "result": {"status": "SUCCESS", "conversation_id": "c-42"}}),
        ]
        text, conv_id, err = parse_stream_json_lines(lines)
        self.assertEqual(text, "Hello world!")
        self.assertEqual(conv_id, "c-42")
        self.assertIsNone(err)

    def test_runner_strips_bot_token_and_captures_conversation_id(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            fake_agy = Path(tmpdir) / "fake_agy.py"
            fake_agy.write_text(
                "\n".join(
                    [
                        f"#!{sys.executable}",
                        "import json, os",
                        "has_tok = 'TELEGRAM_BOT_TOKEN' in os.environ",
                        "print(json.dumps({'event': 'step_update', 'step_update': {'text_delta': f'token_leaked={has_tok}'}}))",
                        "print(json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'conversation_id': 'conv-99'}}))",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_agy.chmod(0o755)

            runner = AgyRunner(agy_bin=str(fake_agy))
            with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "12345:should-not-leak"}):
                res = runner.run_turn("42", "hi", cwd=Path(tmpdir))
            self.assertEqual(res.returncode, 0)
            self.assertEqual(res.text, "token_leaked=False")
            self.assertEqual(res.conversation_id, "conv-99")

    def test_runner_turn_timeout_kills_process_group(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            fake_agy = Path(tmpdir) / "slow_agy.py"
            fake_agy.write_text(
                "\n".join(
                    [
                        f"#!{sys.executable}",
                        "import time",
                        "time.sleep(30)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_agy.chmod(0o755)

            runner = AgyRunner(agy_bin=str(fake_agy))
            res = runner.run_turn(
                "42",
                "hi",
                cwd=Path(tmpdir),
                execution_cfg={"turnTimeoutSeconds": 0.3},
            )
            self.assertTrue(res.timed_out)
            self.assertEqual(res.returncode, 124)
            self.assertIn("timed out", res.error or "")

    def test_runner_cancel_is_non_blocking_even_when_sigterm_ignored(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            fake_agy = Path(tmpdir) / "stubborn_agy.py"
            fake_agy.write_text(
                "\n".join(
                    [
                        f"#!{sys.executable}",
                        "import signal, time",
                        "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
                        "time.sleep(30)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_agy.chmod(0o755)

            runner = AgyRunner(agy_bin=str(fake_agy))
            holder: dict = {}

            def run_it() -> None:
                holder["res"] = runner.run_turn("42", "hi", cwd=Path(tmpdir))

            t = threading.Thread(target=run_it, daemon=True)
            t.start()
            for _ in range(50):
                if runner.is_running("42"):
                    break
                time.sleep(0.02)
            t0 = time.monotonic()
            self.assertTrue(runner.cancel("42"))
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 0.5)
            t.join(timeout=5.0)
            self.assertTrue(holder["res"].cancelled)

    def test_resolve_agy_bin_falls_back_to_local_bin_when_missing_from_path(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            fake_home = Path(tmpdir)
            local_bin = fake_home / ".local" / "bin"
            local_bin.mkdir(parents=True)
            agy_path = local_bin / "agy"
            agy_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            agy_path.chmod(0o755)

            with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}), patch(
                "telegram_agy.agy_runner.shutil.which", return_value=None
            ), patch("telegram_agy.agy_runner.Path.home", return_value=fake_home):
                self.assertEqual(resolve_agy_bin("agy"), str(agy_path))


if __name__ == "__main__":
    unittest.main()
