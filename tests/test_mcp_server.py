"""Tests for the stdio JSON-RPC 2.0 MCP server."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from telegram_agy.access import allow_user
from telegram_agy.config import default_access_config, save_access_config, save_bot_token
from telegram_agy.mcp_server import TelegramMCPServer
from telegram_agy.relay import RelayServer


class MCPServerTests(unittest.TestCase):
    def test_initialize_negotiation_tools_list_and_calls(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            base = Path(tmpdir)
            state_dir = base / "state"
            workspace = base / "ws"
            workspace.mkdir(parents=True)
            secret_outside = base / "secret.txt"
            secret_outside.write_text("top-secret", encoding="utf-8")

            ws_file = workspace / "report.txt"
            ws_file.write_text("ok", encoding="utf-8")

            cfg = default_access_config()
            allow_user(cfg, 42)
            allow_user(cfg, 43)
            save_access_config(state_dir, cfg)
            save_bot_token(state_dir, "12345:secret")

            relay_events: list[dict] = []
            sock_path = state_dir / "relay.sock"
            relay = RelayServer(
                sock_path,
                on_mcp_reply=lambda req: (relay_events.append(req), {"ok": True})[1],
                on_permission_request=lambda req: {"decision": "allow"},
            )
            relay.start()
            try:
                mock_client = MagicMock()
                mock_client.send_message.return_value = [{"message_id": 501}]
                mock_client.send_file.return_value = {"message_id": 502}
                mock_client.set_message_reaction.return_value = True
                mock_client.edit_message_text.return_value = {"message_id": 501}
                mock_client.download_file.return_value = state_dir / "inbox" / "file.txt"

                server = TelegramMCPServer(
                    state_dir=state_dir,
                    client=mock_client,
                    env={
                        "TELEGRAM_AGY_RELAY_SOCK": str(sock_path),
                        "TELEGRAM_AGY_TURN_ID": "turn-abc",
                        "TELEGRAM_AGY_CHAT_ID": "42",
                        "TELEGRAM_AGY_CWD": str(workspace),
                    },
                )

                requests = [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "9999-01-01"},
                    },
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "telegram_reply",
                            "arguments": {
                                "chat_id": "42",
                                "text": "Working...",
                                "files": [str(ws_file)],
                                "final": False,
                            },
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "telegram_reply",
                            "arguments": {"chat_id": "43", "text": "Cross-chat"},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/call",
                        "params": {
                            "name": "telegram_reply",
                            "arguments": {"chat_id": "42", "files": [str(secret_outside)]},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 6,
                        "method": "tools/call",
                        "params": {
                            "name": "telegram_reply",
                            "arguments": {"chat_id": "42", "files": [str(state_dir / ".env")]},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "tools/call",
                        "params": {
                            "name": "telegram_reply",
                            "arguments": {"chat_id": "42", "text": "Final!", "final": True},
                        },
                    },
                ]
                stdin = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
                stdout = io.StringIO()
                rc = server.serve_stdio(stdin=stdin, stdout=stdout)
                self.assertEqual(rc, 0)

                responses = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
                self.assertEqual([r["id"] for r in responses], [1, 2, 3, 4, 5, 6, 7])
                # Clamped unsupported protocolVersion to supported default
                self.assertEqual(responses[0]["result"]["protocolVersion"], "2024-11-05")
                # Workspace file upload succeeds
                self.assertFalse(responses[2]["result"]["isError"])
                # Cross-chat call rejected even though 43 is in allowFrom
                self.assertTrue(responses[3]["result"]["isError"])
                self.assertIn("active turn chat", responses[3]["result"]["content"][0]["text"])
                # File outside workspace/inbox rejected
                self.assertTrue(responses[4]["result"]["isError"])
                self.assertIn("outside workspace/inbox", responses[4]["result"]["content"][0]["text"])
                # Protected state file (.env) rejected
                self.assertTrue(responses[5]["result"]["isError"])
                self.assertIn("protected", responses[5]["result"]["content"][0]["text"])
                # Final reply succeeded
                self.assertFalse(responses[6]["result"]["isError"])

                self.assertEqual(len(relay_events), 2)
                self.assertFalse(relay_events[0]["final"])
                self.assertTrue(relay_events[1]["final"])
            finally:
                relay.stop()


if __name__ == "__main__":
    unittest.main()
