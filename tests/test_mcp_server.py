"""Tests for the stdio JSON-RPC 2.0 MCP server."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from telegram_agy.access import allow_user
from telegram_agy.config import default_access_config, save_access_config
from telegram_agy.mcp_server import TelegramMCPServer
from telegram_agy.relay import RelayServer


class MCPServerTests(unittest.TestCase):
    def test_initialize_tools_list_and_calls(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            state_dir = Path(tmpdir)
            cfg = default_access_config()
            allow_user(cfg, 42)
            save_access_config(state_dir, cfg)

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
                mock_client.set_message_reaction.return_value = True
                mock_client.edit_message_text.return_value = {"message_id": 501}
                mock_client.download_file.return_value = state_dir / "inbox" / "file.txt"

                server = TelegramMCPServer(
                    state_dir=state_dir,
                    client=mock_client,
                    env={
                        "TELEGRAM_AGY_RELAY_SOCK": str(sock_path),
                        "TELEGRAM_AGY_TURN_ID": "turn-abc",
                    },
                )

                requests = [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "telegram_reply",
                            "arguments": {"chat_id": "42", "text": "Done!", "reply_to": 7},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "telegram_reply",
                            "arguments": {"chat_id": "999", "text": "Exfiltrate!"},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/call",
                        "params": {
                            "name": "telegram_react",
                            "arguments": {"chat_id": "42", "message_id": 7, "emoji": "👍"},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 6,
                        "method": "tools/call",
                        "params": {
                            "name": "telegram_edit_message",
                            "arguments": {"chat_id": "42", "message_id": 501, "text": "Updated!"},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "tools/call",
                        "params": {
                            "name": "telegram_download_attachment",
                            "arguments": {"file_id": "file-123", "filename": "data.txt"},
                        },
                    },
                ]
                stdin = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
                stdout = io.StringIO()
                rc = server.serve_stdio(stdin=stdin, stdout=stdout)
                self.assertEqual(rc, 0)

                responses = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
                self.assertEqual([r["id"] for r in responses], [1, 2, 3, 4, 5, 6, 7])
                self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "telegram-agy")
                tool_names = {t["name"] for t in responses[1]["result"]["tools"]}
                self.assertEqual(
                    tool_names,
                    {"telegram_reply", "telegram_react", "telegram_edit_message", "telegram_download_attachment"},
                )
                self.assertFalse(responses[2]["result"]["isError"])
                self.assertTrue(responses[3]["result"]["isError"])  # unauthorized chat 999
                self.assertIn("not authorized", responses[3]["result"]["content"][0]["text"])
                self.assertFalse(responses[4]["result"]["isError"])
                self.assertFalse(responses[5]["result"]["isError"])
                self.assertFalse(responses[6]["result"]["isError"])
                self.assertEqual(len(relay_events), 1)
                self.assertEqual(relay_events[0]["turnId"], "turn-abc")
            finally:
                relay.stop()


if __name__ == "__main__":
    unittest.main()
