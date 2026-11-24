"""Tests for agy_runner command construction, stream-json parsing, and execution."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from telegram_agy.agy_runner import AgyRunner, build_agy_command, parse_stream_json_lines


class AgyRunnerTests(unittest.TestCase):
    def test_build_agy_command_modes(self) -> None:
        cmd_relay = build_agy_command(
            "hello",
            conversation_id="conv-1",
            execution_cfg={"permissionMode": "relay", "sandbox": True, "mode": "auto", "model": "gemini"},
        )
        self.assertIn("--sandbox", cmd_relay)
        self.assertNotIn("--dangerously-skip-permissions", cmd_relay)
        self.assertIn("--conversation", cmd_relay)
        self.assertIn("conv-1", cmd_relay)
        self.assertIn("--model", cmd_relay)

        cmd_sandbox_auto = build_agy_command(
            "hello",
            execution_cfg={"permissionMode": "sandbox-auto", "sandbox": True},
        )
        self.assertIn("--sandbox", cmd_sandbox_auto)
        self.assertIn("--dangerously-skip-permissions", cmd_sandbox_auto)

        cmd_full_auto = build_agy_command(
            "hello",
            execution_cfg={"permissionMode": "full-auto", "sandbox": False},
        )
        self.assertNotIn("--sandbox", cmd_full_auto)
        self.assertIn("--dangerously-skip-permissions", cmd_full_auto)

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

    def test_parse_stream_json_error_result(self) -> None:
        lines = [
            json.dumps({"event": "result", "result": {"status": "ERROR", "error": "quota exceeded"}}),
        ]
        text, _conv_id, err = parse_stream_json_lines(lines)
        self.assertEqual(text, "")
        self.assertEqual(err, "quota exceeded")

    def test_runner_executes_subprocess_and_captures_conversation_id(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            fake_agy = Path(tmpdir) / "fake_agy.py"
            fake_agy.write_text(
                "\n".join(
                    [
                        f"#!{sys.executable}",
                        "import json, sys",
                        "print(json.dumps({'event': 'step_update', 'step_update': {'text_delta': 'OK from fake agy'}}))",
                        "print(json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'conversation_id': 'conv-99'}}))",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_agy.chmod(0o755)

            runner = AgyRunner(agy_bin=str(fake_agy))
            res = runner.run_turn("42", "hi", cwd=Path(tmpdir))
            self.assertEqual(res.returncode, 0)
            self.assertEqual(res.text, "OK from fake agy")
            self.assertEqual(res.conversation_id, "conv-99")
            self.assertIsNone(res.error)


if __name__ == "__main__":
    unittest.main()
