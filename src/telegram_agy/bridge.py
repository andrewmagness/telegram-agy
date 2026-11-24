"""Telegram long-polling bridge daemon connecting Telegram Bot API to agy."""

from __future__ import annotations

import fcntl
import html
import json
import logging
import os
import re
import signal
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .access import evaluate_inbound_message, is_authorized_callback_user
from .agy_runner import VALID_MODES, AgyRunner
from .config import (
    get_inbox_dir,
    load_access_config,
    load_offset,
    load_sessions,
    save_offset,
    update_access_config,
    update_sessions,
)
from .relay import RelayServer
from .telegram_api import TelegramAPIError, TelegramClient

LOGGER = logging.getLogger("telegram_agy.bridge")

_ENVELOPE_TAG_RE = re.compile(r"<(/?)(channel|quoted_reply)\b", re.IGNORECASE)
_INLINE_HTML_LIMIT = 3800


@dataclass
class PendingApproval:
    """In-flight Telegram inline-keyboard approval request."""

    request_id: str
    chat_id: str
    tool_name: str
    event: threading.Event = field(default_factory=threading.Event)
    decision: str = "deny"
    reason: str = ""
    session_allow: bool = False


def make_session_key(chat_id: int | str, thread_id: int | None = None) -> str:
    """Build a stable session key per chat or forum topic."""
    if thread_id is not None:
        return f"{chat_id}:{thread_id}"
    return str(chat_id)


def _format_user_label(sender: dict[str, Any]) -> str:
    username = sender.get("username")
    if username:
        return f"@{username}"
    parts = [sender.get("first_name") or "", sender.get("last_name") or ""]
    full = " ".join(p for p in parts if p).strip()
    return full or str(sender.get("id") or "unknown")


def escape_envelope_text(text: str) -> str:
    """Neutralize XML channel framing tags inside user-controlled text."""
    return _ENVELOPE_TAG_RE.sub(r"&lt;\1\2", text)


def build_channel_envelope(
    message: dict[str, Any],
    *,
    body_text: str,
    attachments: list[str] | None = None,
) -> str:
    """Wrap an inbound Telegram message in a `<channel source="telegram">` envelope."""
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id", "")
    message_id = message.get("message_id", "")
    user_id = sender.get("id", "")
    user_label = _format_user_label(sender)
    thread_id = message.get("message_thread_id")
    date_epoch = message.get("date")
    ts_iso = (
        datetime.fromtimestamp(int(date_epoch), tz=timezone.utc).isoformat()
        if date_epoch
        else datetime.now(tz=timezone.utc).isoformat()
    )

    attrs = [
        'source="telegram"',
        f'chat_id="{html.escape(str(chat_id), quote=True)}"',
        f'message_id="{html.escape(str(message_id), quote=True)}"',
        f'user="{html.escape(user_label, quote=True)}"',
        f'user_id="{html.escape(str(user_id), quote=True)}"',
        f'ts="{html.escape(ts_iso, quote=True)}"',
    ]
    if thread_id is not None:
        attrs.append(f'thread_id="{html.escape(str(thread_id), quote=True)}"')

    lines: list[str] = []
    reply_to = message.get("reply_to_message")
    if isinstance(reply_to, dict):
        quoted = (reply_to.get("text") or reply_to.get("caption") or "").strip()
        if quoted:
            snippet = quoted[:280] + ("..." if len(quoted) > 280 else "")
            reply_sender = reply_to.get("from") or {}
            reply_from_label = _format_user_label(reply_sender)
            q_attrs = (
                f'untrusted="true" '
                f'message_id="{html.escape(str(reply_to.get("message_id", "")), quote=True)}" '
                f'from="{html.escape(reply_from_label, quote=True)}"'
            )
            lines.append(
                f"<quoted_reply {q_attrs}>\n"
                f"{escape_envelope_text(snippet)}\n"
                f"</quoted_reply>"
            )

    if body_text.strip():
        lines.append(escape_envelope_text(body_text.strip()))

    for note in attachments or []:
        lines.append(escape_envelope_text(note))

    content = "\n".join(lines) if lines else "[Empty message]"
    return f"<channel {' '.join(attrs)}>\n{content}\n</channel>"


def _extract_attachments(message: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Return `(kind, file_id, filename)` tuples from a Telegram message."""
    items: list[tuple[str, str, str]] = []
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        best = max(photos, key=lambda p: int(p.get("file_size") or 0))
        if best.get("file_id"):
            items.append(("photo", str(best["file_id"]), f"photo_{best.get('file_unique_id', 'img')}.jpg"))

    for kind in ("document", "voice", "audio", "video", "sticker"):
        obj = message.get(kind)
        if isinstance(obj, dict) and obj.get("file_id"):
            default_ext = ".ogg" if kind == "voice" else ".webp" if kind == "sticker" else ".bin"
            name = str(obj.get("file_name") or f"{kind}_{obj.get('file_unique_id', 'file')}{default_ext}")
            items.append((kind, str(obj["file_id"]), name))
    return items


class TelegramBridge:
    """Coordinates Telegram polling, access control, agy turns, and permission relay."""

    def __init__(
        self,
        state_dir: Path,
        client: TelegramClient,
        *,
        default_cwd: Path | None = None,
        runner: AgyRunner | None = None,
    ) -> None:
        self.state_dir = state_dir
        self.client = client
        self.default_cwd = (default_cwd or Path.cwd()).resolve()
        self.runner = runner or AgyRunner()

        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._bot_id: int | None = None
        self._bot_username: str | None = None

        self._turn_mcp_replied: set[str] = set()
        self._pending_approvals: dict[str, PendingApproval] = {}
        self._session_approved_tools: dict[tuple[str, str], set[str]] = {}

        self.relay_sock_path = self.state_dir / "relay.sock"
        self.relay = RelayServer(
            self.relay_sock_path,
            on_mcp_reply=self._handle_relay_mcp_reply,
            on_permission_request=self._handle_relay_permission_request,
        )

    def _get_effective_cwd(self, session_cfg: dict[str, Any], access_cfg: dict[str, Any]) -> Path:
        raw = str(
            session_cfg.get("cwd")
            or (access_cfg.get("execution") or {}).get("defaultCwd")
            or self.default_cwd
        ).strip()
        candidate = Path(raw).expanduser()
        if candidate.is_dir():
            return candidate.resolve()
        return self.default_cwd

    def _handle_relay_mcp_reply(self, req: dict[str, Any]) -> dict[str, Any]:
        turn_id = str(req.get("turnId") or "").strip()
        is_final = bool(req.get("final", False))
        if turn_id and is_final:
            with self._state_lock:
                self._turn_mcp_replied.add(turn_id)
        return {"ok": True}

    def _build_approval_keyboard(
        self,
        req_id: str,
        *,
        allow_session: bool,
    ) -> dict[str, Any]:
        row = [{"text": "✅ Approve", "callback_data": f"perm:allow:{req_id}"}]
        if allow_session:
            row.append({"text": "🔓 Always (session)", "callback_data": f"perm:session:{req_id}"})
        row.append({"text": "❌ Deny", "callback_data": f"perm:deny:{req_id}"})
        return {"inline_keyboard": [row]}

    def _handle_relay_permission_request(self, req: dict[str, Any]) -> dict[str, Any]:
        chat_id = str(req.get("chatId") or "").strip()
        thread_id = req.get("threadId")
        message_id = req.get("messageId")
        user_id = str(req.get("userId") or chat_id).strip()
        chat_type = str(req.get("chatType") or "").strip()
        tool_name = str(req.get("toolName") or "unknown")
        args = req.get("args") or {}
        cwd = str(req.get("cwd") or "")
        req_id = str(req.get("requestId") or uuid.uuid4().hex[:12])

        if not chat_id:
            return {"decision": "deny", "reason": "Missing chatId for Telegram approval."}

        session_key = make_session_key(chat_id, thread_id)
        approval_key = (session_key, user_id)

        with self._state_lock:
            approved = self._session_approved_tools.get(approval_key, set())
            if tool_name in approved:
                return {"decision": "allow", "reason": "Pre-approved for this session."}
            access_cfg = load_access_config(self.state_dir)
            exec_cfg = access_cfg.get("execution") or {}
            timeout_sec = max(5.0, min(120.0, float(exec_cfg.get("approvalTimeoutSeconds", 120))))
            deny_oversized = bool(exec_cfg.get("denyOversizedApprovals", False))

        args_pretty = json.dumps(args, indent=2, ensure_ascii=False)
        escaped_cwd = html.escape(cwd or "workspace")
        escaped_tool = html.escape(tool_name)
        escaped_args = html.escape(args_pretty)
        inline_html = (
            f"<b>🔐 Permission requested</b> in <code>{escaped_cwd}</code>\n"
            f"<b>Tool:</b> <code>{escaped_tool}</code>\n"
            f"<b>Args:</b>\n<pre><code>{escaped_args}</code></pre>"
        )

        is_group = chat_type in ("group", "supergroup")
        allow_session_btn = not (is_group and tool_name == "run_command")
        keyboard = self._build_approval_keyboard(req_id, allow_session=allow_session_btn)

        approval = PendingApproval(
            request_id=req_id,
            chat_id=chat_id,
            tool_name=tool_name,
        )
        with self._state_lock:
            self._pending_approvals[req_id] = approval

        reply_to = int(message_id) if message_id else None
        tid = int(thread_id) if thread_id is not None else None

        try:
            if len(inline_html) <= _INLINE_HTML_LIMIT:
                self.client.send_message(
                    chat_id,
                    inline_html,
                    reply_to_message_id=reply_to,
                    message_thread_id=tid,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            elif deny_oversized:
                with self._state_lock:
                    self._pending_approvals.pop(req_id, None)
                return {
                    "decision": "deny",
                    "reason": "Tool arguments exceed inline Telegram approval size limit.",
                }
            else:
                # Never silently truncate arguments: attach full JSON (or chunk plain text) first.
                attached = False
                fd, tmp_path_str = tempfile.mkstemp(prefix=f"perm_{req_id}_", suffix=".json")
                tmp_path = Path(tmp_path_str)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fh.write(args_pretty)
                    self.client.send_file(
                        chat_id,
                        tmp_path,
                        caption=f"Full arguments for {tool_name} ({len(args_pretty)} chars)",
                        reply_to_message_id=reply_to,
                        message_thread_id=tid,
                    )
                    attached = True
                except Exception:
                    attached = False
                finally:
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass

                if not attached:
                    self.client.send_message(
                        chat_id,
                        f"Full arguments for {tool_name}:\n{args_pretty}",
                        reply_to_message_id=reply_to,
                        message_thread_id=tid,
                    )

                notice_html = (
                    f"<b>🔐 Permission requested</b> in <code>{escaped_cwd}</code>\n"
                    f"<b>Tool:</b> <code>{escaped_tool}</code>\n"
                    f"⚠️ Full arguments ({len(args_pretty)} chars) sent above — review before approving."
                )
                self.client.send_message(
                    chat_id,
                    notice_html,
                    reply_to_message_id=reply_to,
                    message_thread_id=tid,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
        except Exception as exc:
            with self._state_lock:
                self._pending_approvals.pop(req_id, None)
            return {"decision": "deny", "reason": f"Failed to send Telegram prompt: {exc}"}

        signalled = approval.event.wait(timeout=timeout_sec)
        with self._state_lock:
            self._pending_approvals.pop(req_id, None)
            if signalled and approval.session_allow and approval.decision == "allow":
                if allow_session_btn:
                    self._session_approved_tools.setdefault(approval_key, set()).add(tool_name)

        if not signalled:
            return {
                "decision": "deny",
                "reason": f"Timed out after {int(timeout_sec)}s waiting for Telegram approval.",
            }
        return {"decision": approval.decision, "reason": approval.reason}

    def handle_callback_query(self, callback: dict[str, Any]) -> None:
        """Process inline button taps for permission approvals atomically."""
        cb_id = str(callback.get("id") or "")
        data = str(callback.get("data") or "")
        sender = callback.get("from") or {}
        sender_id = sender.get("id")
        msg = callback.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        msg_id = msg.get("message_id")

        if not data.startswith("perm:") or chat_id is None or sender_id is None:
            if cb_id:
                self.client.answer_callback_query(cb_id)
            return

        parts = data.split(":", 2)
        if len(parts) != 3:
            if cb_id:
                self.client.answer_callback_query(cb_id, text="Invalid action.")
            return
        _, action, req_id = parts

        user_label = _format_user_label(sender)
        status_label = ""
        tool_name = "tool"
        outcome = "ok"

        with self._state_lock:
            access_cfg = load_access_config(self.state_dir)
            authorized = is_authorized_callback_user(access_cfg, chat_id, sender_id)
            if not authorized:
                outcome = "unauthorized"
            else:
                approval = self._pending_approvals.get(req_id)
                if (
                    approval is None
                    or approval.event.is_set()
                    or str(approval.chat_id) != str(chat_id)
                ):
                    outcome = "expired"
                else:
                    tool_name = approval.tool_name
                    if action in ("allow", "session"):
                        approval.decision = "allow"
                        approval.session_allow = action == "session"
                        approval.reason = f"Approved by {user_label}"
                        scope = "session" if approval.session_allow else "once"
                        status_label = f"✅ Approved ({scope}) by {user_label}"
                    else:
                        approval.decision = "deny"
                        approval.session_allow = False
                        approval.reason = f"Denied by {user_label}"
                        status_label = f"❌ Denied by {user_label}"
                    approval.event.set()
                    self._pending_approvals.pop(req_id, None)

        if outcome == "unauthorized":
            if cb_id:
                self.client.answer_callback_query(cb_id, text="Not authorized.", show_alert=True)
            return

        if outcome == "expired":
            if cb_id:
                self.client.answer_callback_query(cb_id, text="Approval request expired.")
            return

        if cb_id:
            try:
                self.client.answer_callback_query(cb_id, text=status_label)
            except TelegramAPIError:
                pass
        if msg_id:
            orig_text = msg.get("text") or f"Tool {tool_name}"
            try:
                self.client.edit_message_text(
                    chat_id,
                    int(msg_id),
                    f"{orig_text}\n\n{status_label}",
                )
            except TelegramAPIError:
                pass

    def _clear_session_approvals(self, session_key: str) -> None:
        for key in [k for k in self._session_approved_tools if k[0] == session_key]:
            self._session_approved_tools.pop(key, None)

    def _handle_bot_command(
        self,
        text: str,
        *,
        chat_id: int | str,
        sender_id: int | str,
        thread_id: int | None,
        message_id: int | None,
    ) -> bool:
        """Handle local bridge slash commands. Returns True if handled."""
        stripped = text.strip()
        if not stripped.startswith("/"):
            return False

        parts = stripped.split(maxsplit=1)
        raw_cmd = parts[0].split("@", 1)[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        session_key = make_session_key(chat_id, thread_id)

        if raw_cmd not in ("/new", "/status", "/cd", "/model", "/mode", "/cancel", "/help"):
            return False

        reply_body: str | None = None

        if raw_cmd == "/cancel":
            cancelled = self.runner.cancel(session_key)
            reply_body = "Cancelled active agy turn." if cancelled else "No active turn is running."
        elif raw_cmd == "/help":
            reply_body = (
                "Available commands:\n"
                "/new — Start a fresh agy conversation\n"
                "/status — Show workspace, conversation ID, model, and mode\n"
                "/cd <path> — Switch workspace directory\n"
                "/model [name] — View or change model\n"
                "/mode [auto|accept-edits|plan] — View or change execution mode\n"
                "/cancel — Abort current agy turn\n"
                "/help — Show this message"
            )
        else:
            with self._state_lock:
                access_cfg = load_access_config(self.state_dir)
                sessions = load_sessions(self.state_dir)
                sess = dict(sessions.get(session_key) or {})
                exec_cfg = access_cfg.get("execution") or {}

                if raw_cmd == "/new":
                    def _mut_new(all_sess: dict[str, dict[str, Any]]) -> None:
                        entry = dict(all_sess.get(session_key) or {})
                        entry.pop("conversationId", None)
                        all_sess[session_key] = entry

                    update_sessions(self.state_dir, _mut_new)
                    self._clear_session_approvals(session_key)
                    reply_body = "Started a fresh agy conversation for this chat."

                elif raw_cmd == "/status":
                    cwd = self._get_effective_cwd(sess, access_cfg)
                    conv = sess.get("conversationId") or "(none yet)"
                    model = sess.get("model") or exec_cfg.get("model") or "(default)"
                    mode = sess.get("mode") or exec_cfg.get("mode") or "auto"
                    perm = exec_cfg.get("permissionMode", "relay")
                    sandbox = exec_cfg.get("sandbox", True)
                    running = self.runner.is_running(session_key)
                    reply_body = (
                        f"Session: {session_key}\n"
                        f"Workspace: {cwd}\n"
                        f"Conversation: {conv}\n"
                        f"Model: {model}\n"
                        f"Mode: {mode}\n"
                        f"Permissions: {perm} (sandbox={sandbox})\n"
                        f"Running: {running}"
                    )

                elif raw_cmd == "/cd":
                    allow_from = {str(x) for x in access_cfg.get("allowFrom", [])}
                    if str(sender_id) not in allow_from:
                        reply_body = "Only global allowlisted owners may change workspace directories."
                    elif not arg:
                        reply_body = f"Current workspace: {self._get_effective_cwd(sess, access_cfg)}"
                    else:
                        target = Path(arg).expanduser()
                        if not target.is_absolute():
                            target = self._get_effective_cwd(sess, access_cfg) / target
                        target = target.resolve()
                        allowed_roots = [
                            Path(r).expanduser().resolve()
                            for r in (exec_cfg.get("allowedRoots") or [])
                            if str(r).strip()
                        ]
                        if allowed_roots and not any(
                            target == root or target.is_relative_to(root) for root in allowed_roots
                        ):
                            reply_body = f"Workspace {target} is outside configured allowedRoots."
                        elif not target.is_dir():
                            reply_body = f"Directory does not exist: {target}"
                        else:
                            def _mut_cd(all_sess: dict[str, dict[str, Any]]) -> None:
                                entry = dict(all_sess.get(session_key) or {})
                                entry["cwd"] = str(target)
                                entry.pop("conversationId", None)
                                all_sess[session_key] = entry

                            update_sessions(self.state_dir, _mut_cd)
                            self._clear_session_approvals(session_key)
                            reply_body = (
                                f"Workspace switched to {target} (started fresh conversation)."
                            )

                elif raw_cmd == "/model":
                    if not arg:
                        cur = sess.get("model") or exec_cfg.get("model") or "(default)"
                        reply_body = (
                            f"Current model: {cur}\n"
                            "Use `/model <name>` or `/model default` to change."
                        )
                    elif arg.startswith("-"):
                        reply_body = "Invalid model name (cannot start with '-')."
                    else:
                        new_model = "" if arg.lower() in ("default", "reset", "none") else arg

                        def _mut_model(all_sess: dict[str, dict[str, Any]]) -> None:
                            entry = dict(all_sess.get(session_key) or {})
                            entry["model"] = new_model
                            all_sess[session_key] = entry

                        update_sessions(self.state_dir, _mut_model)
                        reply_body = f"Model set to {new_model or '(default)'}."

                elif raw_cmd == "/mode":
                    if not arg:
                        cur = sess.get("mode") or exec_cfg.get("mode") or "auto"
                        reply_body = (
                            f"Current mode: {cur}\nValid modes: {', '.join(VALID_MODES)}."
                        )
                    elif arg not in VALID_MODES:
                        reply_body = f"Invalid mode. Choose one of: {', '.join(VALID_MODES)}."
                    else:
                        def _mut_mode(all_sess: dict[str, dict[str, Any]]) -> None:
                            entry = dict(all_sess.get(session_key) or {})
                            entry["mode"] = arg
                            all_sess[session_key] = entry

                        update_sessions(self.state_dir, _mut_mode)
                        reply_body = f"Execution mode set to {arg}."

        if reply_body is not None:
            self.client.send_message(
                chat_id,
                reply_body,
                reply_to_message_id=message_id,
                message_thread_id=thread_id,
            )
        return True

    def handle_message(self, message: dict[str, Any], *, async_turn: bool = True) -> None:
        """Process an inbound Telegram message update."""
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = chat.get("id")
        sender_id = sender.get("id")
        message_id = message.get("message_id")
        thread_id = message.get("message_thread_id")

        with self._state_lock:
            def _eval(cfg: dict[str, Any]):
                return evaluate_inbound_message(
                    cfg,
                    message,
                    bot_id=self._bot_id,
                    bot_username=self._bot_username,
                )

            access_cfg, decision = update_access_config(self.state_dir, _eval)

        if decision.reply_text and chat_id is not None:
            try:
                self.client.send_message(
                    chat_id,
                    decision.reply_text,
                    reply_to_message_id=int(message_id) if message_id else None,
                    message_thread_id=int(thread_id) if thread_id is not None else None,
                )
            except TelegramAPIError as exc:
                LOGGER.warning("Failed sending pairing reply: %s", exc)
        if not decision.allowed or chat_id is None or sender_id is None:
            return

        body_text = str(message.get("text") or message.get("caption") or "")
        if self._handle_bot_command(
            body_text,
            chat_id=chat_id,
            sender_id=sender_id,
            thread_id=int(thread_id) if thread_id is not None else None,
            message_id=int(message_id) if message_id else None,
        ):
            return

        ack_emoji = str(access_cfg.get("ackReaction") or "").strip()
        if ack_emoji and message_id is not None:
            try:
                self.client.set_message_reaction(chat_id, int(message_id), ack_emoji)
            except TelegramAPIError:
                pass

        if async_turn:
            worker = threading.Thread(
                target=self._run_agy_turn,
                args=(message, body_text),
                daemon=True,
            )
            worker.start()
        else:
            self._run_agy_turn(message, body_text)

    def _run_agy_turn(self, message: dict[str, Any], body_text: str) -> None:
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = chat["id"]
        chat_type = str(chat.get("type") or "")
        user_id = str(sender.get("id") or "")
        message_id = message.get("message_id")
        thread_id = message.get("message_thread_id")
        session_key = make_session_key(chat_id, int(thread_id) if thread_id is not None else None)
        turn_id = uuid.uuid4().hex

        # Download attachments off the polling thread so slow files don't stall other chats.
        attachment_notes: list[str] = []
        inbox_dir = get_inbox_dir(self.state_dir)
        for kind, file_id, fname in _extract_attachments(message):
            try:
                saved = self.client.download_file(file_id, inbox_dir, filename=fname)
                attachment_notes.append(f"[Attachment ({kind}) saved at {saved}]")
            except Exception as exc:
                attachment_notes.append(
                    f"[Attachment ({kind}, file_id={file_id}) download failed: {exc}]"
                )

        envelope = build_channel_envelope(
            message,
            body_text=body_text,
            attachments=attachment_notes,
        )

        with self._state_lock:
            access_cfg = load_access_config(self.state_dir)
            sessions = load_sessions(self.state_dir)
            sess_cfg = dict(sessions.get(session_key) or {})
            exec_cfg = dict(access_cfg.get("execution") or {})
            cwd = self._get_effective_cwd(sess_cfg, access_cfg)
            conv_id = sess_cfg.get("conversationId")
            chunk_limit = max(64, min(4096, int(access_cfg.get("textChunkLimit") or 4096)))
            reply_to_mode = str(access_cfg.get("replyToMode") or "reply")

        typing_stop = threading.Event()

        def typing_loop() -> None:
            while not typing_stop.is_set():
                try:
                    self.client.send_chat_action(
                        chat_id,
                        "typing",
                        message_thread_id=int(thread_id) if thread_id is not None else None,
                    )
                except Exception:
                    pass
                typing_stop.wait(4.0)

        typing_thread = threading.Thread(target=typing_loop, daemon=True)
        typing_thread.start()

        extra_env = {
            "TELEGRAM_AGY_RELAY_SOCK": str(self.relay_sock_path),
            "TELEGRAM_AGY_STATE_DIR": str(self.state_dir),
            "TELEGRAM_AGY_CHAT_ID": str(chat_id),
            "TELEGRAM_AGY_CHAT_TYPE": chat_type,
            "TELEGRAM_AGY_USER_ID": user_id,
            "TELEGRAM_AGY_CWD": str(cwd),
            "TELEGRAM_AGY_MESSAGE_ID": str(message_id or ""),
            "TELEGRAM_AGY_THREAD_ID": str(thread_id) if thread_id is not None else "",
            "TELEGRAM_AGY_TURN_ID": turn_id,
            "TELEGRAM_AGY_PERMISSION_MODE": str(exec_cfg.get("permissionMode") or "relay"),
        }

        try:
            result = self.runner.run_turn(
                session_key,
                envelope,
                cwd=cwd,
                conversation_id=conv_id,
                execution_cfg=exec_cfg,
                session_cfg=sess_cfg,
                extra_env=extra_env,
            )
        finally:
            typing_stop.set()
            typing_thread.join(timeout=1.0)

        with self._state_lock:
            already_replied_final = turn_id in self._turn_mcp_replied
            self._turn_mcp_replied.discard(turn_id)
            if result.conversation_id and not result.cancelled:
                def _save_conv(all_sess: dict[str, dict[str, Any]]) -> None:
                    cur = dict(all_sess.get(session_key) or {})
                    cur["conversationId"] = result.conversation_id
                    cur.setdefault("cwd", str(cwd))
                    all_sess[session_key] = cur

                update_sessions(self.state_dir, _save_conv)

        if result.cancelled or already_replied_final:
            return

        if result.error and not result.text:
            reply_body = f"agy error: {result.error}"
        elif result.error and result.text:
            reply_body = f"{result.text}\n\n[agy exited with error: {result.error}]"
        else:
            reply_body = result.text or "(No response from agy.)"

        reply_to = int(message_id) if (message_id is not None and reply_to_mode == "reply") else None
        try:
            self.client.send_markdown(
                chat_id,
                reply_body,
                reply_to_message_id=reply_to,
                message_thread_id=int(thread_id) if thread_id is not None else None,
                chunk_limit=chunk_limit,
            )
        except TelegramAPIError as exc:
            LOGGER.error("Failed to send agy response to chat %s: %s", chat_id, exc)

    def handle_update(self, update: dict[str, Any], *, async_turn: bool = True) -> None:
        """Dispatch a single Telegram Update object."""
        if "callback_query" in update and isinstance(update["callback_query"], dict):
            self.handle_callback_query(update["callback_query"])
            return
        msg = update.get("message")
        if isinstance(msg, dict):
            self.handle_message(msg, async_turn=async_turn)

    def stop(self) -> None:
        self._stop_event.set()

    def run_forever(self) -> None:
        """Acquire singleton lock, start relay socket, and poll Telegram Bot API."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_dir / "bridge.lock"
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(lock_fd)
            raise RuntimeError(
                f"Another telegram-agy bridge instance is already running for {self.state_dir}."
            ) from exc

        try:
            me = self.client.get_me()
            self._bot_id = int(me["id"]) if "id" in me else None
            self._bot_username = str(me.get("username") or "")
            LOGGER.info("Connected as @%s (id=%s)", self._bot_username, self._bot_id)

            self.relay.start()
            offset: int | None = load_offset(self.state_dir)

            def _on_signal(_signum: int, _frame: Any) -> None:
                self.stop()

            old_int = signal.getsignal(signal.SIGINT)
            old_term = signal.getsignal(signal.SIGTERM)
            if threading.current_thread() is threading.main_thread():
                signal.signal(signal.SIGINT, _on_signal)
                signal.signal(signal.SIGTERM, _on_signal)

            try:
                while not self._stop_event.is_set():
                    try:
                        updates = self.client.get_updates(
                            offset=offset,
                            timeout=25,
                            allowed_updates=["message", "callback_query"],
                        )
                    except TelegramAPIError as exc:
                        LOGGER.warning("getUpdates error: %s", exc)
                        self._stop_event.wait(3.0)
                        continue

                    for upd in updates:
                        if not isinstance(upd, dict):
                            continue
                        try:
                            self.handle_update(upd, async_turn=True)
                        except Exception as exc:  # pragma: no cover
                            LOGGER.exception("Unhandled error processing update: %s", exc)
                        upd_id = upd.get("update_id")
                        if upd_id is not None:
                            offset = int(upd_id) + 1
                            save_offset(self.state_dir, offset)
            finally:
                self.relay.stop()
                if threading.current_thread() is threading.main_thread():
                    signal.signal(signal.SIGINT, old_int)
                    signal.signal(signal.SIGTERM, old_term)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
