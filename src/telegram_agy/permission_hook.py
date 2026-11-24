"""PreToolUse hook for relaying tool approvals to Telegram."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, TextIO

from .config import DEFAULT_SAFE_TOOLS, get_state_dir, load_access_config
from .relay import send_relay_request

_PATH_ARG_KEYS = {
    "path",
    "file_path",
    "targetfile",
    "destination",
    "target",
    "file",
    "absolutepath",
    "directorypath",
    "searchpath",
    "searchdirectory",
    "directory",
    "dir",
}
_READ_TOOLS = {
    "read_file",
    "view_file",
    "view_file_outline",
    "view_code_item",
    "list_dir",
    "find_by_name",
    "grep_search",
}
_TELEGRAM_MCP_PREFIXES = (
    "mcp_telegram-agy_",
    "mcp_telegram_agy_",
    "telegram-agy_",
    "telegram-agy_telegram-agy_",
    "telegram_agy_",
)


def _iter_string_items(value: Any, *, key_hint: str = "") -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    if isinstance(value, str):
        items.append((key_hint, value))
    elif isinstance(value, list):
        for elem in value:
            items.extend(_iter_string_items(elem, key_hint=key_hint))
    elif isinstance(value, dict):
        for k, v in value.items():
            items.extend(_iter_string_items(v, key_hint=str(k)))
    return items


def _targets_state_dir(args: Any, state_dir: Path, *, cwd: str = "") -> bool:
    """Return True if *args* appears to access *state_dir* outside `inbox/`."""
    resolved_state = state_dir.expanduser().resolve()
    resolved_inbox = (resolved_state / "inbox").resolve()
    base_cwd = Path(cwd).expanduser().resolve() if cwd else Path.cwd().resolve()
    state_markers = {
        str(resolved_state),
        str(state_dir),
        "~/.gemini/channels/telegram",
        ".gemini/channels/telegram",
        "$TELEGRAM_AGY_STATE_DIR",
        "${TELEGRAM_AGY_STATE_DIR}",
    }

    for key, text in _iter_string_items(args):
        raw = text.strip()
        if not raw:
            continue
        is_path_arg = key.lower() in _PATH_ARG_KEYS
        if is_path_arg or raw.startswith(("/", "~", "./", "../")):
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = base_cwd / candidate
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate
            if resolved == resolved_state or resolved.is_relative_to(resolved_state):
                if not resolved.is_relative_to(resolved_inbox):
                    return True

        for marker in state_markers:
            if marker in raw:
                inbox_marker = f"{marker.rstrip('/')}/inbox"
                stripped = raw.replace(inbox_marker, "")
                if marker in stripped:
                    return True
    return False


def _read_targets_within_allowed_roots(
    args: Any,
    *,
    workspace_cwd: str,
    inbox_dir: Path,
    fallback_cwd: str,
) -> bool:
    """Return True if all path-like arguments in *args* stay within workspace or inbox."""
    allowed_roots = [inbox_dir.expanduser().resolve()]
    if workspace_cwd:
        allowed_roots.append(Path(workspace_cwd).expanduser().resolve())
    elif fallback_cwd:
        allowed_roots.append(Path(fallback_cwd).expanduser().resolve())

    base_cwd = (
        Path(workspace_cwd or fallback_cwd).expanduser().resolve()
        if (workspace_cwd or fallback_cwd)
        else Path.cwd().resolve()
    )

    for key, text in _iter_string_items(args):
        key_l = key.lower()
        is_path_key = key_l in _PATH_ARG_KEYS or any(
            tok in key_l for tok in ("path", "dir", "file")
        )
        raw = text.strip()
        if not is_path_key or not raw:
            continue
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = base_cwd / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            return False
        if not any(resolved == root or resolved.is_relative_to(root) for root in allowed_roots):
            return False
    return True


def _matches_telegram_tool(tool_name: str, base_name: str) -> bool:
    if tool_name == base_name:
        return True
    return any(tool_name == f"{prefix}{base_name}" for prefix in _TELEGRAM_MCP_PREFIXES)


def _is_safe_tool(tool_name: str, safe_tools: set[str]) -> bool:
    if not tool_name:
        return False
    if tool_name in safe_tools:
        return True
    for entry in safe_tools:
        if entry.startswith("telegram_") and _matches_telegram_tool(tool_name, entry):
            return True
    return False


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
        # Per Antigravity's PreToolUse contract (https://antigravity.google/docs/hooks):
        # "allow" delegates to the next hook / Antigravity's default permission policy,
        # whereas "auto_approve" bypasses interactive prompts and {} is rejected as deny.
        return {"decision": "allow"}

    resolved_state = state_dir or get_state_dir(
        environ.get("TELEGRAM_AGY_STATE_DIR"), create=False
    )
    access_cfg = load_access_config(resolved_state)
    exec_cfg = access_cfg.get("execution") or {}
    safe_tools = set(exec_cfg.get("safeTools") or DEFAULT_SAFE_TOOLS)

    tool_call = payload.get("toolCall") or {}
    tool_name = str(tool_call.get("name") or "")
    args = tool_call.get("args") or {}
    cwd = str(payload.get("cwd") or "")

    # Block any tool (including read tools) from accessing state_dir outside inbox/.
    if _targets_state_dir(args, resolved_state, cwd=cwd):
        return {
            "decision": "deny",
            "reason": "Accessing telegram-agy state directory is prohibited.",
        }

    perm_mode = environ.get("TELEGRAM_AGY_PERMISSION_MODE", "relay").strip()
    if perm_mode in ("sandbox-auto", "full-auto"):
        return {"decision": "auto_approve"}

    is_telegram_reply = _matches_telegram_tool(tool_name, "telegram_reply")
    is_safe = _is_safe_tool(tool_name, safe_tools)

    if is_telegram_reply:
        files = args.get("files") if isinstance(args, dict) else None
        if not files:
            return {"decision": "auto_approve"}
    elif is_safe:
        if tool_name in _READ_TOOLS:
            workspace_cwd = environ.get("TELEGRAM_AGY_CWD", "").strip()
            inbox_dir = resolved_state / "inbox"
            if _read_targets_within_allowed_roots(
                args,
                workspace_cwd=workspace_cwd,
                inbox_dir=inbox_dir,
                fallback_cwd=cwd,
            ):
                return {"decision": "auto_approve"}
        else:
            return {"decision": "auto_approve"}

    chat_id = environ.get("TELEGRAM_AGY_CHAT_ID", "")
    thread_id = environ.get("TELEGRAM_AGY_THREAD_ID", "")
    message_id = environ.get("TELEGRAM_AGY_MESSAGE_ID", "")
    user_id = environ.get("TELEGRAM_AGY_USER_ID", "")
    chat_type = environ.get("TELEGRAM_AGY_CHAT_TYPE", "")
    timeout_sec = max(5.0, min(120.0, float(exec_cfg.get("approvalTimeoutSeconds", 120))))

    request_payload = {
        "type": "permission_request",
        "requestId": uuid.uuid4().hex[:12],
        "chatId": chat_id,
        "threadId": int(thread_id) if thread_id.lstrip("-").isdigit() else None,
        "messageId": int(message_id) if message_id.isdigit() else None,
        "userId": user_id,
        "chatType": chat_type,
        "toolName": tool_name or "unknown_tool",
        "args": args,
        "cwd": cwd,
    }

    try:
        response = send_relay_request(relay_sock, request_payload, timeout=timeout_sec + 5.0)
    except Exception as exc:
        return {
            "decision": "deny",
            "reason": f"Telegram approval relay unavailable or timed out: {exc}",
        }

    decision = response.get("decision")
    if decision in ("allow", "auto_approve"):
        out: dict[str, Any] = {"decision": "auto_approve"}
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
        out_stream.write(json.dumps({"decision": "allow"}) + "\n")
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
