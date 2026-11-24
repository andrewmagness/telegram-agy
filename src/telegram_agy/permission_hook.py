"""PreToolUse hook for relaying guarded tool approvals to Telegram."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, TextIO

from .config import DEFAULT_GUARDED_TOOLS, get_state_dir, load_access_config
from .relay import send_relay_request


def evaluate_hook_payload(
    payload: dict[str, Any],
    *,
    env: dict[str, str] | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Evaluate a `PreToolUse` hook payload and return the JSON decision object."""
    environ = os.environ if env is None else env
    relay_sock = environ.get("TELEGRAM_AGY_RELAY_SOCK", "").strip()
    if not relay_sock:
        # Normal interactive agy CLI session outside the Telegram bridge: no-op.
        return {}

    perm_mode = environ.get("TELEGRAM_AGY_PERMISSION_MODE", "relay").strip()
    if perm_mode in ("sandbox-auto", "full-auto"):
        return {"decision": "allow"}

    resolved_state = state_dir or get_state_dir(environ.get("TELEGRAM_AGY_STATE_DIR"))
    access_cfg = load_access_config(resolved_state)
    exec_cfg = access_cfg.get("execution") or {}
    guarded = set(exec_cfg.get("guardedTools") or DEFAULT_GUARDED_TOOLS)

    tool_call = payload.get("toolCall") or {}
    tool_name = str(tool_call.get("name") or "")
    if tool_name and tool_name not in guarded:
        return {"decision": "allow"}

    chat_id = environ.get("TELEGRAM_AGY_CHAT_ID", "")
    thread_id = environ.get("TELEGRAM_AGY_THREAD_ID", "")
    message_id = environ.get("TELEGRAM_AGY_MESSAGE_ID", "")
    timeout_sec = float(exec_cfg.get("approvalTimeoutSeconds", 120))

    request_payload = {
        "type": "permission_request",
        "requestId": uuid.uuid4().hex[:12],
        "chatId": chat_id,
        "threadId": int(thread_id) if thread_id.lstrip("-").isdigit() else None,
        "messageId": int(message_id) if message_id.isdigit() else None,
        "toolName": tool_name or "unknown_tool",
        "args": tool_call.get("args") or {},
        "cwd": payload.get("cwd") or "",
    }

    try:
        response = send_relay_request(relay_sock, request_payload, timeout=timeout_sec + 5.0)
    except Exception as exc:
        return {
            "decision": "deny",
            "reason": f"Telegram approval relay unavailable or timed out: {exc}",
        }

    decision = response.get("decision")
    if decision == "allow":
        out: dict[str, Any] = {"decision": "allow"}
        if response.get("reason"):
            out["reason"] = str(response["reason"])
        return out

    return {
        "decision": "deny",
        "reason": str(response.get("reason") or "Denied via Telegram permission relay."),
    }


def run_hook(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """Read PreToolUse stdin JSON, evaluate it, and write decision JSON to stdout."""
    in_stream = sys.stdin if stdin is None else stdin
    out_stream = sys.stdout if stdout is None else stdout
    raw = in_stream.read().strip()
    if not raw:
        out_stream.write("{}\n")
        out_stream.flush()
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        out_stream.write(
            json.dumps({"decision": "deny", "reason": "Invalid PreToolUse JSON payload."}) + "\n"
        )
        out_stream.flush()
        return 0

    result = evaluate_hook_payload(payload if isinstance(payload, dict) else {})
    out_stream.write(json.dumps(result) + "\n")
    out_stream.flush()
    return 0
