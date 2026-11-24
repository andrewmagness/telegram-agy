"""Tests for the PreToolUse permission relay hook."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from telegram_agy.config import default_access_config, save_access_config
from telegram_agy.permission_hook import evaluate_hook_payload
from telegram_agy.relay import RelayServer


class PermissionHookTests(unittest.TestCase):
    def test_passthrough_outside_telegram_session_returns_allow(self) -> None:
        res = evaluate_hook_payload(
            {"toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}},
            env={},
        )
        self.assertEqual(res, {"decision": "allow"})

    def test_sandbox_auto_allows_except_state_dir_tampering(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            state_dir = Path(tmpdir) / "state"
            save_access_config(state_dir, default_access_config())
            env = {
                "TELEGRAM_AGY_RELAY_SOCK": str(state_dir / "unused.sock"),
                "TELEGRAM_AGY_STATE_DIR": str(state_dir),
                "TELEGRAM_AGY_PERMISSION_MODE": "sandbox-auto",
            }
            ok = evaluate_hook_payload(
                {"toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}},
                env=env,
                state_dir=state_dir,
            )
            self.assertEqual(ok, {"decision": "auto_approve"})

            tamper = evaluate_hook_payload(
                {
                    "toolCall": {
                        "name": "write_file",
                        "args": {"Path": str(state_dir / "access.json"), "Content": "{}"},
                    }
                },
                env=env,
                state_dir=state_dir,
            )
            self.assertEqual(tamper["decision"], "deny")

    def test_relay_allowlist_inversion_prompts_unknown_tools_and_file_replies(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            state_dir = Path(tmpdir)
            save_access_config(state_dir, default_access_config())
            sock_path = state_dir / "relay.sock"

            seen_requests: list[dict] = []

            def on_perm(req: dict) -> dict:
                seen_requests.append(req)
                if req["toolName"] in ("run_command", "invoke_subagent"):
                    return {"decision": "allow", "reason": "Approved by @alice"}
                return {"decision": "deny", "reason": "Denied by @alice"}

            relay = RelayServer(
                sock_path,
                on_mcp_reply=lambda _r: {"ok": True},
                on_permission_request=on_perm,
            )
            relay.start()
            try:
                env = {
                    "TELEGRAM_AGY_RELAY_SOCK": str(sock_path),
                    "TELEGRAM_AGY_STATE_DIR": str(state_dir),
                    "TELEGRAM_AGY_CHAT_ID": "42",
                    "TELEGRAM_AGY_USER_ID": "42",
                    "TELEGRAM_AGY_CHAT_TYPE": "private",
                    "TELEGRAM_AGY_PERMISSION_MODE": "relay",
                }
                # Safe read-only tool -> auto_approve locally without hitting relay
                ro = evaluate_hook_payload(
                    {"toolCall": {"name": "read_file", "args": {"Path": "/tmp/a"}}},
                    env=env,
                    state_dir=state_dir,
                )
                self.assertEqual(ro, {"decision": "auto_approve"})
                self.assertEqual(len(seen_requests), 0)

                # Text-only telegram_reply -> auto_approve without hitting relay
                txt_reply = evaluate_hook_payload(
                    {"toolCall": {"name": "mcp_telegram_agy_telegram_reply", "args": {"chat_id": "42", "text": "hi"}}},
                    env=env,
                    state_dir=state_dir,
                )
                self.assertEqual(txt_reply, {"decision": "auto_approve"})
                self.assertEqual(len(seen_requests), 0)

                # telegram_reply WITH files -> must prompt on relay!
                file_reply = evaluate_hook_payload(
                    {
                        "toolCall": {
                            "name": "mcp_telegram_agy_telegram_reply",
                            "args": {"chat_id": "42", "files": ["/tmp/out.txt"]},
                        }
                    },
                    env=env,
                    state_dir=state_dir,
                )
                self.assertEqual(file_reply["decision"], "deny")
                self.assertEqual(len(seen_requests), 1)

                # Unknown/non-safe tool (e.g., invoke_subagent or third-party MCP) -> prompts!
                sub = evaluate_hook_payload(
                    {"toolCall": {"name": "invoke_subagent", "args": {"Prompt": "do work"}}},
                    env=env,
                    state_dir=state_dir,
                )
                self.assertEqual(sub["decision"], "auto_approve")
                self.assertEqual(len(seen_requests), 2)

                # Reading inbox attachment is allowed; writing access.json is hard-denied
                inbox_read = evaluate_hook_payload(
                    {"toolCall": {"name": "read_file", "args": {"Path": str(state_dir / "inbox" / "a.png")}}},
                    env=env,
                    state_dir=state_dir,
                )
                self.assertEqual(inbox_read, {"decision": "auto_approve"})

                env_read = evaluate_hook_payload(
                    {"toolCall": {"name": "run_command", "args": {"CommandLine": f"cat {state_dir}/.env"}}},
                    env=env,
                    state_dir=state_dir,
                )
                self.assertEqual(env_read["decision"], "deny")
                self.assertEqual(len(seen_requests), 2)
            finally:
                relay.stop()

    def test_relay_mode_fails_closed_when_socket_missing(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            state_dir = Path(tmpdir)
            save_access_config(state_dir, default_access_config())
            res = evaluate_hook_payload(
                {"toolCall": {"name": "run_command", "args": {"CommandLine": "rm -rf /"}}},
                env={
                    "TELEGRAM_AGY_RELAY_SOCK": str(state_dir / "missing.sock"),
                    "TELEGRAM_AGY_STATE_DIR": str(state_dir),
                    "TELEGRAM_AGY_PERMISSION_MODE": "relay",
                },
                state_dir=state_dir,
            )
            self.assertEqual(res["decision"], "deny")


if __name__ == "__main__":
    unittest.main()
