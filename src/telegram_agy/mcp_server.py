"""Zero-dependency stdio JSON-RPC 2.0 MCP server for Telegram tools."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .access import is_authorized_chat
from .config import get_inbox_dir, get_state_dir, load_access_config, load_bot_token
from .relay import send_relay_request
from .telegram_api import TelegramClient

SUPPORTED_PROTOCOL_VERSION = "2024-11-05"

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "telegram_reply",
        "description": (
            "Send a message and/or local files/images back to an authorized Telegram chat. "
            "Automatically chunks long messages to Telegram's 4096-character limit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "chat_id": {
                    "type": "string",
                    "description": "Target Telegram chat ID.",
                },
                "text": {
                    "type": "string",
                    "description": "Message text to send.",
                },
                "reply_to": {
                    "type": "integer",
                    "description": "Optional Telegram message_id to reply to.",
                },
                "thread_id": {
                    "type": "integer",
                    "description": "Optional forum topic message_thread_id.",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of local file paths to send as photos/documents.",
                },
                "parse_mode": {
                    "type": "string",
                    "description": "Optional Telegram parse_mode (e.g., Markdown or HTML).",
                },
            },
            "required": ["chat_id"],
        },
    },
    {
        "name": "telegram_react",
        "description": "Add an emoji reaction to a Telegram message.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Telegram chat ID."},
                "message_id": {"type": "integer", "description": "Target message ID."},
                "emoji": {"type": "string", "description": "Standard Telegram emoji (e.g., 👍)."},
            },
            "required": ["chat_id", "message_id", "emoji"],
        },
    },
    {
        "name": "telegram_edit_message",
        "description": "Edit a message previously sent by the bot in an authorized Telegram chat.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Telegram chat ID."},
                "message_id": {"type": "integer", "description": "Bot message ID to edit."},
                "text": {"type": "string", "description": "New message text."},
                "parse_mode": {"type": "string", "description": "Optional parse_mode."},
            },
            "required": ["chat_id", "message_id", "text"],
        },
    },
    {
        "name": "telegram_download_attachment",
        "description": "Download a Telegram file by file_id into the local inbox directory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "Telegram file_id."},
                "filename": {"type": "string", "description": "Optional preferred filename."},
            },
            "required": ["file_id"],
        },
    },
]


class TelegramMCPServer:
    """Handles MCP JSON-RPC 2.0 requests for Telegram operations."""

    def __init__(
        self,
        *,
        state_dir: Path | None = None,
        client: TelegramClient | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.environ = os.environ if env is None else env
        self.state_dir = state_dir or get_state_dir(
            self.environ.get("TELEGRAM_AGY_STATE_DIR"), create=False
        )
        self._client = client

    def _get_client(self) -> TelegramClient:
        if self._client is not None:
            return self._client
        token = load_bot_token(self.state_dir)
        if not token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN is not configured. Run `telegram-agy configure` first."
            )
        return TelegramClient(token)

    def _require_authorized_chat(self, chat_id: str | int) -> None:
        access_cfg = load_access_config(self.state_dir)
        if not is_authorized_chat(access_cfg, chat_id):
            raise PermissionError(
                f"Chat {chat_id} is not authorized in {self.state_dir / 'access.json'}."
            )

    def _notify_relay_reply_sent(self) -> None:
        relay_sock = self.environ.get("TELEGRAM_AGY_RELAY_SOCK", "").strip()
        turn_id = self.environ.get("TELEGRAM_AGY_TURN_ID", "").strip()
        if not relay_sock or not turn_id:
            return
        try:
            send_relay_request(
                relay_sock,
                {"type": "mcp_reply_sent", "turnId": turn_id},
                timeout=2.0,
            )
        except Exception:
            pass

    def handle_tool_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute one MCP tool call and return an MCP tool result dict."""
        client = self._get_client()

        if name == "telegram_reply":
            chat_id = str(arguments.get("chat_id", "")).strip()
            if not chat_id:
                raise ValueError("chat_id is required.")
            self._require_authorized_chat(chat_id)
            text = str(arguments.get("text") or "")
            reply_to = arguments.get("reply_to")
            thread_id = arguments.get("thread_id")
            parse_mode = arguments.get("parse_mode")
            files = arguments.get("files") or []

            sent_ids: list[int] = []
            if text:
                msgs = client.send_message(
                    chat_id,
                    text,
                    reply_to_message_id=int(reply_to) if reply_to is not None else None,
                    message_thread_id=int(thread_id) if thread_id is not None else None,
                    parse_mode=str(parse_mode) if parse_mode else None,
                )
                sent_ids.extend(int(m["message_id"]) for m in msgs if isinstance(m, dict) and "message_id" in m)

            for file_item in files:
                file_path = Path(str(file_item)).expanduser().resolve()
                if not file_path.is_file():
                    raise FileNotFoundError(f"Attachment file not found: {file_path}")
                res = client.send_file(
                    chat_id,
                    file_path,
                    reply_to_message_id=int(reply_to) if reply_to is not None and not sent_ids else None,
                    message_thread_id=int(thread_id) if thread_id is not None else None,
                )
                if isinstance(res, dict) and "message_id" in res:
                    sent_ids.append(int(res["message_id"]))

            self._notify_relay_reply_sent()
            summary = f"Sent to chat {chat_id} (message_ids={sent_ids})."
            return {"content": [{"type": "text", "text": summary}], "isError": False}

        if name == "telegram_react":
            chat_id = str(arguments.get("chat_id", "")).strip()
            self._require_authorized_chat(chat_id)
            message_id = int(arguments["message_id"])
            emoji = str(arguments["emoji"])
            client.set_message_reaction(chat_id, message_id, emoji)
            return {
                "content": [{"type": "text", "text": f"Reacted {emoji} on message {message_id}."}],
                "isError": False,
            }

        if name == "telegram_edit_message":
            chat_id = str(arguments.get("chat_id", "")).strip()
            self._require_authorized_chat(chat_id)
            message_id = int(arguments["message_id"])
            text = str(arguments["text"])
            parse_mode = arguments.get("parse_mode")
            client.edit_message_text(
                chat_id,
                message_id,
                text,
                parse_mode=str(parse_mode) if parse_mode else None,
            )
            return {
                "content": [{"type": "text", "text": f"Edited message {message_id}."}],
                "isError": False,
            }

        if name == "telegram_download_attachment":
            file_id = str(arguments.get("file_id", "")).strip()
            if not file_id:
                raise ValueError("file_id is required.")
            filename = arguments.get("filename")
            inbox = get_inbox_dir(self.state_dir)
            saved_path = client.download_file(
                file_id,
                inbox,
                filename=str(filename) if filename else None,
            )
            return {
                "content": [{"type": "text", "text": f"Saved attachment to {saved_path}"}],
                "isError": False,
            }

        raise KeyError(f"Unknown tool: {name}")

    def handle_jsonrpc(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Process one JSON-RPC 2.0 request and return a response (or None for notifications)."""
        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        # Notifications have no id and expect no response.
        if req_id is None:
            return None

        try:
            if method == "initialize":
                client_proto = params.get("protocolVersion") or SUPPORTED_PROTOCOL_VERSION
                result = {
                    "protocolVersion": str(client_proto),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "telegram-agy", "version": __version__},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOL_DEFINITIONS}
            elif method == "tools/call":
                tool_name = str(params.get("name", ""))
                args = params.get("arguments") or {}
                try:
                    result = self.handle_tool_call(tool_name, args)
                except Exception as tool_exc:
                    result = {
                        "content": [{"type": "text", "text": f"Error: {tool_exc}"}],
                        "isError": True,
                    }
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": str(exc)},
            }

    def serve_stdio(
        self,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
    ) -> int:
        """Run the newline-delimited JSON-RPC loop over stdio."""
        in_stream = sys.stdin if stdin is None else stdin
        out_stream = sys.stdout if stdout is None else stdout

        for raw_line in in_stream:
            line = raw_line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                err_resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                }
                out_stream.write(json.dumps(err_resp) + "\n")
                out_stream.flush()
                continue

            if not isinstance(msg, dict):
                continue
            resp = self.handle_jsonrpc(msg)
            if resp is not None:
                out_stream.write(json.dumps(resp) + "\n")
                out_stream.flush()
        return 0
