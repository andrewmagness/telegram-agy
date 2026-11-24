"""Pure-stdlib Telegram Bot API client, multipart uploader, and chunker."""

from __future__ import annotations

import json
import mimetypes
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

TELEGRAM_API_BASE = "https://api.telegram.org"
MAX_TEXT_CHUNK = 4096
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

_FENCE_RE = re.compile(r"^```([A-Za-z0-9_+-]*)\s*$")
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class TelegramAPIError(RuntimeError):
    """Raised when the Telegram Bot API returns an error."""

    def __init__(self, method: str, status: int, description: str) -> None:
        self.method = method
        self.status = status
        self.description = description
        super().__init__(f"Telegram API {method} failed ({status}): {description}")


def sanitize_filename(name: str, fallback: str = "attachment.bin") -> str:
    """Strip directory components and unsafe characters from an attachment filename."""
    base = Path(name or "").name.strip()
    cleaned = _UNSAFE_FILENAME_RE.sub("_", base).strip("._")
    if not cleaned:
        return fallback
    return cleaned[:120]


def chunk_text(text: str, limit: int = MAX_TEXT_CHUNK) -> list[str]:
    """Split *text* into <= *limit*-char chunks, keeping fenced code blocks valid."""
    if not text:
        return [""]
    if limit < 64:
        limit = 64
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    open_fence_lang: str | None = None

    while remaining:
        prefix = f"```{open_fence_lang}\n" if open_fence_lang is not None else ""
        # Reserve room for a closing fence if we're inside or might leave an open fence.
        reserve_close = 4  # "\n```"
        effective_limit = max(32, limit - len(prefix) - reserve_close)

        if len(remaining) <= effective_limit:
            piece = prefix + remaining
            chunks.append(piece)
            break

        window = remaining[:effective_limit]
        split_at = -1
        for sep in ("\n\n", "\n", " "):
            idx = window.rfind(sep)
            if idx > effective_limit // 4:
                split_at = idx + len(sep)
                break
        if split_at <= 0:
            split_at = effective_limit

        raw_piece = remaining[:split_at]
        remaining = remaining[split_at:]

        # Track fenced code blocks across this piece.
        state = open_fence_lang
        for line in raw_piece.splitlines():
            stripped = line.rstrip()
            if stripped.startswith("```"):
                if state is None:
                    match = _FENCE_RE.match(stripped)
                    state = match.group(1) if match else ""
                else:
                    state = None

        piece = prefix + raw_piece
        if state is not None:
            if not piece.endswith("\n"):
                piece += "\n"
            piece += "```"
        chunks.append(piece.rstrip("\n") if state is None else piece)
        open_fence_lang = state

    return [c for c in chunks if c]


class TelegramClient:
    """Minimal synchronous Telegram Bot API client using urllib."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = TELEGRAM_API_BASE,
        timeout: float = 35.0,
    ) -> None:
        if not token or ":" not in token:
            raise ValueError("Invalid Telegram bot token format.")
        self.token = token.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _api_url(self, method: str) -> str:
        return f"{self.base_url}/bot{self.token}/{method}"

    def _file_url(self, file_path: str) -> str:
        clean = file_path.lstrip("/")
        return f"{self.base_url}/file/bot{self.token}/{clean}"

    def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Invoke a Telegram Bot API JSON method and return `result`."""
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            self._api_url(method),
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._execute(method, req, timeout=timeout or self.timeout)

    def call_multipart(
        self,
        method: str,
        fields: dict[str, Any],
        file_field: str,
        file_path: Path,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Invoke a Telegram Bot API multipart/form-data upload method."""
        boundary = f"----telegramagy{uuid.uuid4().hex}"
        body = bytearray()

        for key, value in fields.items():
            if value is None:
                continue
            val_str = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
            body.extend(val_str.encode("utf-8"))
            body.extend(b"\r\n")

        mime_type, _ = mimetypes.guess_type(str(file_path))
        content_type = mime_type or "application/octet-stream"
        safe_name = sanitize_filename(file_path.name)
        file_bytes = file_path.read_bytes()

        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            f'Content-Disposition: form-data; name="{file_field}"; filename="{safe_name}"\r\n'.encode(
                "utf-8"
            )
        )
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        body.extend(file_bytes)
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))

        req = urllib.request.Request(
            self._api_url(method),
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        return self._execute(method, req, timeout=timeout or max(self.timeout, 60.0))

    def _execute(
        self,
        method: str,
        req: urllib.request.Request,
        *,
        timeout: float,
    ) -> Any:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            status = exc.code
            try:
                err_json = json.loads(raw)
                desc = err_json.get("description", raw)
            except json.JSONDecodeError:
                desc = raw
            raise TelegramAPIError(method, status, desc) from exc
        except urllib.error.URLError as exc:
            raise TelegramAPIError(method, 0, str(exc.reason)) from exc

        parsed = json.loads(raw)
        if not parsed.get("ok"):
            raise TelegramAPIError(
                method,
                int(parsed.get("error_code", status)),
                str(parsed.get("description", "Unknown Telegram error")),
            )
        return parsed.get("result")

    def get_me(self) -> dict[str, Any]:
        return self.call("getMe")

    def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int = 25,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        if allowed_updates is not None:
            payload["allowed_updates"] = allowed_updates
        return self.call("getUpdates", payload, timeout=float(timeout) + 10.0)

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        chunk_limit: int = MAX_TEXT_CHUNK,
    ) -> list[dict[str, Any]]:
        """Send text (auto-chunked to Telegram's 4096-char limit)."""
        chunks = chunk_text(text, limit=chunk_limit)
        sent: list[dict[str, Any]] = []
        for idx, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if idx == 0 and reply_to_message_id is not None:
                payload["reply_to_message_id"] = reply_to_message_id
            if message_thread_id is not None:
                payload["message_thread_id"] = message_thread_id
            if parse_mode:
                payload["parse_mode"] = parse_mode
            if idx == len(chunks) - 1 and reply_markup is not None:
                payload["reply_markup"] = reply_markup

            try:
                res = self.call("sendMessage", payload)
            except TelegramAPIError as exc:
                # Retry cleanly without parse_mode on formatting 400 errors.
                if exc.status == 400 and "parse_mode" in payload:
                    payload.pop("parse_mode", None)
                    res = self.call("sendMessage", payload)
                else:
                    raise
            sent.append(res)
        return sent

    def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:MAX_TEXT_CHUNK],
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            return self.call("editMessageText", payload)
        except TelegramAPIError as exc:
            if exc.status == 400 and "parse_mode" in payload:
                payload.pop("parse_mode", None)
                return self.call("editMessageText", payload)
            raise

    def set_message_reaction(
        self,
        chat_id: int | str,
        message_id: int,
        emoji: str,
    ) -> bool:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": emoji}] if emoji else [],
        }
        return bool(self.call("setMessageReaction", payload))

    def send_chat_action(
        self,
        chat_id: int | str,
        action: str = "typing",
        *,
        message_thread_id: int | None = None,
    ) -> bool:
        payload: dict[str, Any] = {"chat_id": chat_id, "action": action}
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        return bool(self.call("sendChatAction", payload))

    def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
    ) -> bool:
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text:
            payload["text"] = text[:200]
        return bool(self.call("answerCallbackQuery", payload))

    def send_file(
        self,
        chat_id: int | str,
        file_path: Path,
        *,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> dict[str, Any]:
        """Send an image via sendPhoto or other file via sendDocument."""
        ext = file_path.suffix.lower()
        is_photo = ext in {".png", ".jpg", ".jpeg", ".webp"}
        method = "sendPhoto" if is_photo else "sendDocument"
        field = "photo" if is_photo else "document"
        fields: dict[str, Any] = {
            "chat_id": chat_id,
            "caption": caption[:1024] if caption else None,
            "reply_to_message_id": reply_to_message_id,
            "message_thread_id": message_thread_id,
        }
        return self.call_multipart(method, fields, field, file_path)

    def get_file(self, file_id: str) -> dict[str, Any]:
        return self.call("getFile", {"file_id": file_id})

    def download_file(
        self,
        file_id: str,
        dest_dir: Path,
        *,
        filename: str | None = None,
        max_bytes: int = MAX_DOWNLOAD_BYTES,
    ) -> Path:
        """Download a Telegram file into *dest_dir* with size limit & safe filename."""
        info = self.get_file(file_id)
        remote_path = info.get("file_path")
        if not remote_path:
            raise TelegramAPIError("getFile", 400, "Telegram did not return a file_path.")
        file_size = info.get("file_size")
        if file_size is not None and int(file_size) > max_bytes:
            raise ValueError(f"File exceeds {max_bytes} byte limit ({file_size} bytes).")

        inferred_name = filename or Path(remote_path).name or "attachment.bin"
        safe_name = sanitize_filename(inferred_name)
        dest_dir.mkdir(parents=True, exist_ok=True)
        unique_name = f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{safe_name}"
        target = dest_dir / unique_name

        req = urllib.request.Request(self._file_url(remote_path), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=60.0) as resp:
                total = 0
                chunks: list[bytes] = []
                while True:
                    block = resp.read(65536)
                    if not block:
                        break
                    total += len(block)
                    if total > max_bytes:
                        raise ValueError(f"Downloaded file exceeds {max_bytes} byte limit.")
                    chunks.append(block)
        except urllib.error.HTTPError as exc:
            raise TelegramAPIError("downloadFile", exc.code, str(exc.reason)) from exc
        except urllib.error.URLError as exc:
            raise TelegramAPIError("downloadFile", 0, str(exc.reason)) from exc

        target.write_bytes(b"".join(chunks))
        return target
