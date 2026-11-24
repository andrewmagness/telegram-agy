"""Local Unix domain socket relay for MCP reply tracking and PreToolUse approvals."""

from __future__ import annotations

import json
import os
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any


class RelayServer:
    """Threaded AF_UNIX line-delimited JSON server used by the bridge daemon."""

    def __init__(
        self,
        socket_path: Path,
        *,
        on_mcp_reply: Callable[[dict[str, Any]], dict[str, Any]],
        on_permission_request: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        self.socket_path = socket_path
        self._on_mcp_reply = on_mcp_reply
        self._on_permission_request = on_permission_request
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        sock.listen(32)
        sock.settimeout(0.5)
        self._sock = sock
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._serve_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass

    def _serve_loop(self) -> None:
        assert self._sock is not None
        while not self._stop_event.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            worker = threading.Thread(target=self._handle_conn, args=(conn,), daemon=True)
            worker.start()

    def _handle_conn(self, conn: socket.socket) -> None:
        with conn:
            try:
                file_obj = conn.makefile("r", encoding="utf-8")
                line = file_obj.readline()
                if not line:
                    return
                req = json.loads(line)
                msg_type = req.get("type")
                if msg_type == "mcp_reply_sent":
                    resp = self._on_mcp_reply(req)
                elif msg_type == "permission_request":
                    resp = self._on_permission_request(req)
                else:
                    resp = {"ok": False, "error": f"Unknown relay request type: {msg_type}"}
            except Exception as exc:  # pragma: no cover - defensive error boundary
                resp = {"ok": False, "error": str(exc)}

            try:
                conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
            except OSError:
                pass


def send_relay_request(
    socket_path: str | Path,
    payload: dict[str, Any],
    *,
    timeout: float = 130.0,
) -> dict[str, Any]:
    """Send a single JSON-line request to the bridge's Unix relay socket."""
    path_str = str(socket_path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(path_str)
        client.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        reader = client.makefile("r", encoding="utf-8")
        line = reader.readline()
        if not line:
            raise RuntimeError("Relay socket closed without response.")
        return json.loads(line)
