"""End-to-end mocked tests for TelegramBridge."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from telegram_agy.access import allow_user, approve_pairing
from telegram_agy.agy_runner import AgyResult
from telegram_agy.bridge import TelegramBridge, build_channel_envelope
from telegram_agy.config import default_access_config, load_access_config, load_sessions, save_access_config
from telegram_agy.relay import send_relay_request


class BridgeTests(unittest.TestCase):
    def _make_bridge(self, state_dir: Path, workspace: Path) -> tuple[TelegramBridge, MagicMock, MagicMock]:
        client = MagicMock()
        client.send_message.return_value = [{"message_id": 777}]
        runner = MagicMock()
        runner.is_running.return_value = False
        bridge = TelegramBridge(state_dir, client, default_cwd=workspace, runner=runner)
        return bridge, client, runner

    def test_build_channel_envelope_includes_reply_and_attachments(self) -> None:
        msg = {
            "message_id": 55,
            "date": 1700000000,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "username": "alice"},
            "reply_to_message": {"message_id": 50, "text": "older context"},
        }
        env = build_channel_envelope(msg, body_text="fix this", attachments=["[Attachment (photo) saved at /tmp/a.jpg]"])
        self.assertIn('<channel source="telegram"', env)
        self.assertIn('chat_id="42"', env)
        self.assertIn('user="@alice"', env)
        self.assertIn("[Replying to message 50: older context]", env)
        self.assertIn("fix this", env)
        self.assertIn("[Attachment (photo) saved at /tmp/a.jpg]", env)

    def test_pairing_then_authorized_turn_and_session_persistence(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            state_dir = Path(tmpdir) / "state"
            workspace = Path(tmpdir) / "ws"
            workspace.mkdir()
            save_access_config(state_dir, default_access_config())

            bridge, client, runner = self._make_bridge(state_dir, workspace)
            runner.run_turn.return_value = AgyResult(text="Tests pass!", conversation_id="conv-abc")

            dm = {
                "message_id": 1,
                "date": 1700000000,
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "is_bot": False, "username": "alice"},
                "text": "hello",
            }
            # First DM triggers pairing code and never invokes agy
            bridge.handle_message(dm, async_turn=False)
            runner.run_turn.assert_not_called()
            client.send_message.assert_called_once()

            cfg = load_access_config(state_dir)
            code = next(iter(cfg["pending"]))
            approve_pairing(cfg, code)
            save_access_config(state_dir, cfg)

            client.reset_mock()
            bridge.handle_message({**dm, "message_id": 2, "text": "run unit tests"}, async_turn=False)
            runner.run_turn.assert_called_once()
            self.assertIn("run unit tests", runner.run_turn.call_args.args[1])
            client.send_message.assert_called_once()
            self.assertEqual(client.send_message.call_args.args[1], "Tests pass!")

            sessions = load_sessions(state_dir)
            self.assertEqual(sessions["42"]["conversationId"], "conv-abc")

    def test_mcp_reply_deduplication(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            state_dir = Path(tmpdir) / "state"
            workspace = Path(tmpdir) / "ws"
            workspace.mkdir()
            cfg = default_access_config()
            allow_user(cfg, 42)
            save_access_config(state_dir, cfg)

            bridge, client, runner = self._make_bridge(state_dir, workspace)
            bridge.relay.start()
            try:
                def fake_run_turn(_key, _prompt, *, extra_env, **_kwargs):
                    send_relay_request(
                        extra_env["TELEGRAM_AGY_RELAY_SOCK"],
                        {"type": "mcp_reply_sent", "turnId": extra_env["TELEGRAM_AGY_TURN_ID"]},
                    )
                    return AgyResult(text="Already sent via MCP", conversation_id="c1")

                runner.run_turn.side_effect = fake_run_turn
                dm = {
                    "message_id": 5,
                    "date": 1700000000,
                    "chat": {"id": 42, "type": "private"},
                    "from": {"id": 42, "is_bot": False, "username": "alice"},
                    "text": "send file",
                }
                bridge.handle_message(dm, async_turn=False)
                client.send_message.assert_not_called()
            finally:
                bridge.relay.stop()

    def test_slash_commands_cd_status_new_and_cancel(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            state_dir = Path(tmpdir) / "state"
            ws1 = Path(tmpdir) / "ws1"
            ws2 = Path(tmpdir) / "ws2"
            ws1.mkdir()
            ws2.mkdir()
            cfg = default_access_config()
            allow_user(cfg, 42)
            save_access_config(state_dir, cfg)

            bridge, client, runner = self._make_bridge(state_dir, ws1)
            base_msg = {
                "message_id": 10,
                "date": 1700000000,
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "is_bot": False, "username": "alice"},
            }

            bridge.handle_message({**base_msg, "text": f"/cd {ws2}"}, async_turn=False)
            self.assertIn(str(ws2.resolve()), client.send_message.call_args.args[1])

            bridge.handle_message({**base_msg, "text": "/status"}, async_turn=False)
            self.assertIn(str(ws2.resolve()), client.send_message.call_args.args[1])

            runner.cancel.return_value = True
            bridge.handle_message({**base_msg, "text": "/cancel"}, async_turn=False)
            runner.cancel.assert_called_once_with("42")
            self.assertIn("Cancelled", client.send_message.call_args.args[1])

    def test_permission_relay_callback_approval_flow(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            state_dir = Path(tmpdir) / "state"
            workspace = Path(tmpdir) / "ws"
            workspace.mkdir()
            cfg = default_access_config()
            allow_user(cfg, 42)
            save_access_config(state_dir, cfg)

            bridge, client, _runner = self._make_bridge(state_dir, workspace)

            result_holder: dict = {}

            def ask_perm() -> None:
                result_holder["resp"] = bridge._handle_relay_permission_request(
                    {
                        "requestId": "req123",
                        "chatId": "42",
                        "toolName": "run_command",
                        "args": {"CommandLine": "ls"},
                        "cwd": str(workspace),
                    }
                )

            t = threading.Thread(target=ask_perm, daemon=True)
            t.start()
            # Wait briefly until pending approval is registered
            for _ in range(50):
                if "req123" in bridge._pending_approvals:
                    break
                threading.Event().wait(0.02)

            # Unauthorized user tapping Approve is rejected
            bridge.handle_callback_query(
                {
                    "id": "cb-bad",
                    "data": "perm:session:req123",
                    "from": {"id": 999, "username": "eve"},
                    "message": {"message_id": 777, "chat": {"id": 42}},
                }
            )
            self.assertTrue(t.is_alive())

            # Authorized user taps Always (session)
            bridge.handle_callback_query(
                {
                    "id": "cb-ok",
                    "data": "perm:session:req123",
                    "from": {"id": 42, "username": "alice"},
                    "message": {"message_id": 777, "chat": {"id": 42}, "text": "Permission requested"},
                }
            )
            t.join(timeout=2.0)
            self.assertEqual(result_holder["resp"]["decision"], "allow")

            # Subsequent request for same tool is auto-approved for the session
            client.reset_mock()
            res2 = bridge._handle_relay_permission_request(
                {"requestId": "req456", "chatId": "42", "toolName": "run_command", "args": {}}
            )
            self.assertEqual(res2["decision"], "allow")
            client.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
