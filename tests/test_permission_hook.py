"""Tests for the PreToolUse permission relay hook."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from telegram_agy.config import default_access_config, save_access_config
from telegram_agy.permission_hook import evaluate_hook_payload
from telegram_agy.relay import RelayServer


class PermissionHookTests(unittest.TestCase):
    def test_passthrough_when_outside_telegram_session(self) -> None:
        res = evaluate_hook_payload(
            {"toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}},
            env={},
        )
        self.assertEqual(res, {})

    def test_sandbox_auto_allows_without_relay_prompt(self) -> None:
        res = evaluate_hook_payload(
            {"toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}},
            env={
                "TELEGRAM_AGY_RELAY_SOCK": "/tmp/unused.sock",
                "TELEGRAM_AGY_PERMISSION_MODE": "sandbox-auto",
            },
        )
        self.assertEqual(res, {"decision": "allow"})

    def test_relay_mode_auto_allows_unguarded_and_relays_guarded(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            state_dir = Path(tmpdir)
            save_access_config(state_dir, default_access_config())
            sock_path = state_dir / "relay.sock"

            seen_requests: list[dict] = []

            def on_perm(req: dict) -> dict:
                seen_requests.append(req)
                if req["toolName"] == "run_command":
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
                    "TELEGRAM_AGY_PERMISSION_MODE": "relay",
                }
                # Read-only tool -> allowed locally without hitting relay
                ro = evaluate_hook_payload(
                    {"toolCall": {"name": "read_file", "args": {"Path": "/tmp/a"}}},
                    env=env,
                    state_dir=state_dir,
                )
                self.assertEqual(ro, {"decision": "allow"})
                self.assertEqual(len(seen_requests), 0)

                # Guarded tool -> relayed and allowed
                rw_allow = evaluate_hook_payload(
                    {"toolCall": {"name": "run_command", "args": {"CommandLine": "pytest"}}},
                    env=env,
                    state_dir=state_dir,
                )
                self.assertEqual(rw_allow["decision"], "allow")
                self.assertEqual(len(seen_requests), 1)

                # Guarded tool -> relayed and denied
                rw_deny = evaluate_hook_payload(
                    {"toolCall": {"name": "write_file", "args": {"Path": "/tmp/x"}}},
                    env=env,
                    state_dir=state_dir,
                )
                self.assertEqual(rw_deny["decision"], "deny")
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
