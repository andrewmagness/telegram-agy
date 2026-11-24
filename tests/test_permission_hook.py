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

            # Both write_file and read_file on state_dir/.env and $TELEGRAM_AGY_STATE_DIR are denied
            for call in (
                {"name": "write_file", "args": {"Path": str(state_dir / "access.json"), "Content": "{}"}},
                {"name": "read_file", "args": {"Path": str(state_dir / ".env")}},
                {"name": "run_command", "args": {"CommandLine": "cat $TELEGRAM_AGY_STATE_DIR/.env"}},
            ):
                res = evaluate_hook_payload({"toolCall": call}, env=env, state_dir=state_dir)
                self.assertEqual(res["decision"], "deny", f"Expected deny for {call}")

    def test_relay_read_confinement_strict_mcp_prefix_and_search_web_prompt(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            base = Path(tmpdir)
            state_dir = base / "state"
            workspace = base / "ws"
            workspace.mkdir(parents=True)
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
                    "TELEGRAM_AGY_CWD": str(workspace),
                    "TELEGRAM_AGY_CHAT_ID": "42",
                    "TELEGRAM_AGY_USER_ID": "42",
                    "TELEGRAM_AGY_CHAT_TYPE": "private",
                    "TELEGRAM_AGY_PERMISSION_MODE": "relay",
                }
                # Read inside workspace -> auto_approve without hitting relay
                ws_read = evaluate_hook_payload(
                    {"toolCall": {"name": "read_file", "args": {"Path": str(workspace / "a.py")}}},
                    env=env,
                    state_dir=state_dir,
                )
                self.assertEqual(ws_read, {"decision": "auto_approve"})
                self.assertEqual(len(seen_requests), 0)

                # Read inside inbox -> auto_approve without hitting relay
                inbox_read = evaluate_hook_payload(
                    {"toolCall": {"name": "read_file", "args": {"Path": str(state_dir / "inbox" / "a.png")}}},
                    env=env,
                    state_dir=state_dir,
                )
                self.assertEqual(inbox_read, {"decision": "auto_approve"})
                self.assertEqual(len(seen_requests), 0)

                # Read outside workspace/inbox (e.g. ~/.ssh/id_rsa) -> prompts on relay!
                outside_read = evaluate_hook_payload(
                    {"toolCall": {"name": "read_file", "args": {"Path": "/etc/passwd"}}},
                    env=env,
                    state_dir=state_dir,
                )
                self.assertEqual(outside_read["decision"], "deny")
                self.assertEqual(len(seen_requests), 1)

                # Read targeting state_dir/.env -> hard-denied before relay
                env_read = evaluate_hook_payload(
                    {"toolCall": {"name": "read_file", "args": {"Path": str(state_dir / ".env")}}},
                    env=env,
                    state_dir=state_dir,
                )
                self.assertEqual(env_read["decision"], "deny")
                self.assertEqual(len(seen_requests), 1)

                # search_web -> prompts on relay (not in DEFAULT_SAFE_TOOLS)
                web = evaluate_hook_payload(
                    {"toolCall": {"name": "search_web", "args": {"query": "secret"}}},
                    env=env,
                    state_dir=state_dir,
                )
                self.assertEqual(web["decision"], "deny")
                self.assertEqual(len(seen_requests), 2)

                # Third-party evil_telegram_react must NOT match _is_safe_tool
                evil_mcp = evaluate_hook_payload(
                    {"toolCall": {"name": "evil_telegram_react", "args": {"chat_id": "42"}}},
                    env=env,
                    state_dir=state_dir,
                )
                self.assertEqual(evil_mcp["decision"], "deny")
                self.assertEqual(len(seen_requests), 3)

                # Legitimate plugin-prefixed text-only telegram_reply -> auto_approve
                txt_reply = evaluate_hook_payload(
                    {"toolCall": {"name": "mcp_telegram-agy_telegram_reply", "args": {"chat_id": "42", "text": "hi"}}},
                    env=env,
                    state_dir=state_dir,
                )
                self.assertEqual(txt_reply, {"decision": "auto_approve"})
                self.assertEqual(len(seen_requests), 3)

                # telegram_reply WITH files -> prompts on relay
                file_reply = evaluate_hook_payload(
                    {
                        "toolCall": {
                            "name": "mcp_telegram-agy_telegram_reply",
                            "args": {"chat_id": "42", "files": [str(workspace / "out.txt")]},
                        }
                    },
                    env=env,
                    state_dir=state_dir,
                )
                self.assertEqual(file_reply["decision"], "deny")
                self.assertEqual(len(seen_requests), 4)

                # Unknown tool -> prompts on relay
                sub = evaluate_hook_payload(
                    {"toolCall": {"name": "invoke_subagent", "args": {"Prompt": "do work"}}},
                    env=env,
                    state_dir=state_dir,
                )
                self.assertEqual(sub["decision"], "auto_approve")
                self.assertEqual(len(seen_requests), 5)
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
