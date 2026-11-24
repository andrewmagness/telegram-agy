"""Telegram long-polling bridge daemon connecting Telegram Bot API to agy."""

from __future__ import annotations

import html
import json
import logging
import os
import signal
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .access import evaluate_inbound_message, is_authorized_callback_user
from .agy_runner import AgyRunner
from .config import (
    get_inbox_dir,
    load_access_config,
    load_sessions,
    save_access_config,
    save_sessions,
)
from .relay import RelayServer
from .telegram_api import TelegramAPIError, TelegramClient

LOGGER = logging.getLogger("telegram_agy.bridge")


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
            lines.append(f"[Replying to message {reply_to.get('message_id')}: {snippet}]")

    if body_text.strip():
        lines.append(body_text.strip())

    for note in attachments or []:
        lines.append(note)

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
        self._session_approved_tools: dict[str, set[str]] = {}

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
        if turn_id:
            with self._state_lock:
                self._turn_mcp_replied.add(turn_id)
        return {"ok": True}

    def _handle_relay_permission_request(self, req: dict[str, Any]) -> dict[str, Any]:
        chat_id = str(req.get("chatId") or "").strip()
        thread_id = req.get("threadId")
        message_id = req.get("messageId")
        tool_name = str(req.get("toolName") or "unknown")
        args = req.get("args") or {}
        cwd = str(req.get("cwd") or "")
        req_id = str(req.get("requestId") or uuid.uuid4().hex[:12])

        if not chat_id:
            return {"decision": "deny", "reason": "Missing chatId for Telegram approval."}

        session_key = make_session_key(chat_id, thread_id)
        with self._state_lock:
            approved = self._session_approved_tools.get(session_key, set())
            if tool_name in approved:
                return {"decision": "allow", "reason": "Pre-approved for this session."}
            access_cfg = load_access_config(self.state_dir)
            timeout_sec = float((access_cfg.get("execution") or {}).get("approvalTimeoutSeconds", 120))

        args_preview = json.dumps(args, indent=2, ensure_ascii=False)
        if len(args_preview) > 1200:
            args_preview = args_preview[:1200] + "\n..."

        prompt_text = (
            f"🔐 Permission requested in `{cwd or 'workspace'}`\n"
            f"Tool: `{tool_name}`\n"
            f"Args:\n```json\n{args_preview}\n```"
        )
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"perm:allow:{req_id}"},
                    {"text": "🔓 Always (session)", "callback_data": f"perm:session:{req_id}"},
                    {"text": "❌ Deny", "callback_data": f"perm:deny:{req_id}"},
                ]
            ]
        }

        approval = PendingApproval(
            request_id=req_id,
            chat_id=chat_id,
            tool_name=tool_name,
        )
        with self._state_lock:
            self._pending_approvals[req_id] = approval

        try:
            self.client.send_message(
                chat_id,
                prompt_text,
                reply_to_message_id=int(message_id) if message_id else None,
                message_thread_id=int(thread_id) if thread_id is not None else None,
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
                self._session_approved_tools.setdefault(session_key, set()).add(tool_name)

        if not signalled:
            return {"decision": "deny", "reason": f"Timed out after {int(timeout_sec)}s waiting for Telegram approval."}
        return {"decision": approval.decision, "reason": approval.reason}

    def handle_callback_query(self, callback: dict[str, Any]) -> None:
        """Process inline button taps for permission approvals."""
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

        with self._state_lock:
            access_cfg = load_access_config(self.state_dir)
            authorized = is_authorized_callback_user(access_cfg, chat_id, sender_id)
            approval = self._pending_approvals.get(req_id)

        if not authorized:
            if cb_id:
                self.client.answer_callback_query(cb_id, text="Not authorized.", show_alert=True)
            return

        if approval is None or str(approval.chat_id) != str(chat_id):
            if cb_id:
                self.client.answer_callback_query(cb_id, text="Approval request expired.")
            return

        user_label = _format_user_label(sender)
        if action in ("allow", "session"):
            approval.decision = "allow"
            approval.session_allow = action == "session"
            approval.reason = f"Approved by {user_label}"
            status_label = f"✅ Approved ({'session' if approval.session_allow else 'once'}) by {user_label}"
        else:
            approval.decision = "deny"
            approval.reason = f"Denied by {user_label}"
            status_label = f"❌ Denied by {user_label}"

        approval.event.set()
        if cb_id:
            try:
                self.client.answer_callback_query(cb_id, text=status_label)
            except TelegramAPIError:
                pass
        if msg_id:
            orig_text = msg.get("text") or f"Tool `{approval.tool_name}`"
            try:
                self.client.edit_message_text(
                    chat_id,
                    int(msg_id),
                    f"{orig_text}\n\n{status_label}",
                )
            except TelegramAPIError:
                pass

    def _handle_bot_command(
        self,
        text: str,
        *,
        chat_id: int | str,
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

        def reply(body: str) -> None:
            self.client.send_message(
                chat_id,
                body,
                reply_to_message_id=message_id,
                message_thread_id=thread_id,
            )

        with self._state_lock:
            access_cfg = load_access_config(self.state_dir)
            sessions = load_sessions(self.state_dir)
            sess = dict(sessions.get(session_key) or {})

            if raw_cmd == "/new":
                sess.pop("conversationId", None)
                sessions[session_key] = sess
                save_sessions(self.state_dir, sessions)
                self._session_approved_tools.pop(session_key, None)
                reply("Started a fresh agy conversation for this chat.")
                return True

            if raw_cmd == "/status":
                exec_cfg = access_cfg.get("execution") or {}
                cwd = self._get_effective_cwd(sess, access_cfg)
                conv = sess.get("conversationId") or "(none yet)"
                model = sess.get("model") or exec_cfg.get("model") or "(default)"
                mode = sess.get("mode") or exec_cfg.get("mode") or "auto"
                perm = exec_cfg.get("permissionMode", "relay")
                sandbox = exec_cfg.get("sandbox", True)
                running = self.runner.is_running(session_key)
                reply(
                    f"Session: {session_key}\n"
                    f"Workspace: {cwd}\n"
                    f"Conversation: {conv}\n"
                    f"Model: {model}\n"
                    f"Mode: {mode}\n"
                    f"Permissions: {perm} (sandbox={sandbox})\n"
                    f"Running: {running}"
                )
                return True

            if raw_cmd == "/cd":
                if not arg:
                    reply(f"Current workspace: {self._get_effective_cwd(sess, access_cfg)}")
                    return True
                target = Path(arg).expanduser()
                if not target.is_absolute():
                    target = self._get_effective_cwd(sess, access_cfg) / target
                target = target.resolve()
                if not target.is_dir():
                    reply(f"Directory does not exist: {target}")
                    return True
                sess["cwd"] = str(target)
                sess.pop("conversationId", None)
                sessions[session_key] = sess
                save_sessions(self.state_dir, sessions)
                self._session_approved_tools.pop(session_key, None)
                reply(f"Workspace switched to {target} (started fresh conversation).")
                return True

            if raw_cmd == "/model":
                if not arg:
                    cur = sess.get("model") or (access_cfg.get("execution") or {}).get("model") or "(default)"
                    reply(f"Current model: {cur}\nUse `/model <name>` or `/model default` to change.")
                    return True
                sess["model"] = "" if arg.lower() in ("default", "reset", "none") else arg
                sessions[session_key] = sess
                save_sessions(self.state_dir, sessions)
                reply(f"Model set to {sess['model'] or '(default)'}.")
                return True

            if raw_cmd == "/mode":
                if not arg:
                    cur = sess.get("mode") or (access_cfg.get("execution") or {}).get("mode") or "auto"
                    reply(f"Current mode: {cur}\nValid modes: auto, accept-edits, plan.")
                    return True
                if arg not in ("auto", "accept-edits", "plan"):
                    reply("Invalid mode. Choose one of: auto, accept-edits, plan.")
                    return True
                sess["mode"] = arg
                sessions[session_key] = sess
                save_sessions(self.state_dir, sessions)
                reply(f"Execution mode set to {arg}.")
                return True

            if raw_cmd == "/cancel":
                cancelled = self.runner.cancel(session_key)
                reply("Cancelled active agy turn." if cancelled else "No active turn is running.")
                return True

            if raw_cmd == "/help":
                reply(
                    "Available commands:\n"
                    "/new — Start a fresh agy conversation\n"
                    "/status — Show workspace, conversation ID, model, and mode\n"
                    "/cd <path> — Switch workspace directory\n"
                    "/model [name] — View or change model\n"
                    "/mode [auto|accept-edits|plan] — View or change execution mode\n"
                    "/cancel — Abort current agy turn\n"
                    "/help — Show this message"
                )
                return True

        return False

    def handle_message(self, message: dict[str, Any], *, async_turn: bool = True) -> None:
        """Process an inbound Telegram message update."""
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        thread_id = message.get("message_thread_id")

        with self._state_lock:
            access_cfg = load_access_config(self.state_dir)
            decision = evaluate_inbound_message(
                access_cfg,
                message,
                bot_id=self._bot_id,
                bot_username=self._bot_username,
            )
            if decision.config_changed:
                save_access_config(self.state_dir, access_cfg)

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
        if not decision.allowed or chat_id is None:
            return

        body_text = str(message.get("text") or message.get("caption") or "")
        if self._handle_bot_command(
            body_text,
            chat_id=chat_id,
            thread_id=int(thread_id) if thread_id is not None else None,
            message_id=int(message_id) if message_id else None,
        ):
            return

        # Best-effort ack reaction.
        ack_emoji = str(access_cfg.get("ackReaction") or "").strip()
        if ack_emoji and message_id is not None:
            try:
                self.client.set_message_reaction(chat_id, int(message_id), ack_emoji)
            except TelegramAPIError:
                pass

        # Download any attachments into inbox/.
        attachment_notes: list[str] = []
        inbox_dir = get_inbox_dir(self.state_dir)
        for kind, file_id, fname in _extract_attachments(message):
            try:
                saved = self.client.download_file(file_id, inbox_dir, filename=fname)
                attachment_notes.append(f"[Attachment ({kind}) saved at {saved}]")
            except Exception as exc:
                attachment_notes.append(f"[Attachment ({kind}, file_id={file_id}) download failed: {exc}]")

        envelope = build_channel_envelope(
            message,
            body_text=body_text,
            attachments=attachment_notes,
        )

        if async_turn:
            worker = threading.Thread(
                target=self._run_agy_turn,
                args=(chat_id, thread_id, message_id, envelope),
                daemon=True,
            )
            worker.start()
        else:
            self._run_agy_turn(chat_id, thread_id, message_id, envelope)

    def _run_agy_turn(
        self,
        chat_id: int | str,
        thread_id: int | None,
        message_id: int | None,
        envelope: str,
    ) -> None:
        session_key = make_session_key(chat_id, thread_id)
        turn_id = uuid.uuid4().hex

        with self._state_lock:
            access_cfg = load_access_config(self.state_dir)
            sessions = load_sessions(self.state_dir)
            sess_cfg = dict(sessions.get(session_key) or {})
            exec_cfg = dict(access_cfg.get("execution") or {})
            cwd = self._get_effective_cwd(sess_cfg, access_cfg)
            conv_id = sess_cfg.get("conversationId")
            chunk_limit = int(access_cfg.get("textChunkLimit") or 4096)
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
            already_replied = turn_id in self._turn_mcp_replied
            self._turn_mcp_replied.discard(turn_id)
            if result.conversation_id and not result.cancelled:
                sessions = load_sessions(self.state_dir)
                cur = dict(sessions.get(session_key) or {})
                cur["conversationId"] = result.conversation_id
                cur.setdefault("cwd", str(cwd))
                sessions[session_key] = cur
                save_sessions(self.state_dir, sessions)

        if result.cancelled or already_replied:
            return

        if result.error and not result.text:
            reply_body = f"agy error: {result.error}"
        elif result.error and result.text:
            reply_body = f"{result.text}\n\n[agy exited with error: {result.error}]"
        else:
            reply_body = result.text or "(No response from agy.)"

        reply_to = int(message_id) if (message_id is not None and reply_to_mode == "reply") else None
        try:
            self.client.send_message(
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
        """Start relay socket and run Telegram Bot API long-polling loop."""
        me = self.client.get_me()
        self._bot_id = int(me["id"]) if "id" in me else None
        self._bot_username = str(me.get("username") or "")
        LOGGER.info("Connected as @%s (id=%s)", self._bot_username, self._bot_id)

        self.relay.start()
        offset: int | None = None

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
                    upd_id = upd.get("update_id")
                    if upd_id is not None:
                        offset = int(upd_id) + 1
                    try:
                        self.handle_update(upd, async_turn=True)
                    except Exception as exc:  # pragma: no cover
                        LOGGER.exception("Unhandled error processing update: %s", exc)
        finally:
            self.relay.stop()
            if threading.current_thread() is threading.main_thread():
                signal.signal(signal.SIGINT, old_int)
                signal.signal(signal.SIGTERM, old_term)
