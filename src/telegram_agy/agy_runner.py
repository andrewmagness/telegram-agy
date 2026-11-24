"""Subprocess runner and stream-json parser for Antigravity CLI (`agy`)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VALID_MODES = ("auto", "accept-edits", "plan")


@dataclass
class AgyResult:
    """Parsed outcome of a single `agy` execution turn."""

    text: str
    conversation_id: str | None = None
    error: str | None = None
    returncode: int = 0
    cancelled: bool = False
    timed_out: bool = False


def build_agy_command(
    prompt: str,
    *,
    conversation_id: str | None = None,
    execution_cfg: dict[str, Any] | None = None,
    session_cfg: dict[str, Any] | None = None,
    agy_bin: str = "agy",
) -> list[str]:
    """Build the `agy` CLI command list for a single non-interactive turn."""
    exec_cfg = execution_cfg or {}
    sess_cfg = session_cfg or {}

    cmd = [agy_bin, "--output-format", "stream-json"]
    if conversation_id:
        conv = str(conversation_id).strip()
        if conv.startswith("-"):
            raise ValueError(f"Invalid conversation ID: {conv!r}")
        cmd.append(f"--conversation={conv}")

    perm_mode = str(exec_cfg.get("permissionMode", "relay") or "relay").strip()
    sandbox = bool(exec_cfg.get("sandbox", True))
    if sandbox:
        cmd.append("--sandbox")
    if perm_mode in ("sandbox-auto", "full-auto"):
        cmd.append("--dangerously-skip-permissions")

    mode = str(sess_cfg.get("mode") or exec_cfg.get("mode") or "").strip()
    if mode:
        if mode.startswith("-") or mode not in VALID_MODES:
            raise ValueError(f"Invalid agy mode: {mode!r}")
        cmd.append(f"--mode={mode}")

    model = str(sess_cfg.get("model") or exec_cfg.get("model") or "").strip()
    if model:
        if model.startswith("-"):
            raise ValueError(f"Invalid model name: {model!r}")
        cmd.append(f"--model={model}")

    for arg in exec_cfg.get("extraArgs") or []:
        if str(arg):
            cmd.append(str(arg))

    cmd.extend(["-p", prompt])
    return cmd


def _extract_text_blocks(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") in (None, "text", "output_text") and isinstance(
                    item.get("text"), str
                ):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "".join(parts)
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if "content" in value:
            return _extract_text_blocks(value["content"])
    return ""


def parse_stream_json_lines(lines: Iterable[str]) -> tuple[str, str | None, str | None]:
    """Parse NDJSON lines emitted by `agy --output-format stream-json`.

    Returns `(text, conversation_id, error)`.
    """
    deltas: list[str] = []
    fallback_text: str = ""
    raw_lines: list[str] = []
    conversation_id: str | None = None
    error_msg: str | None = None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            raw_lines.append(line)
            continue

        if not isinstance(obj, dict):
            continue

        for key in ("conversation_id", "conversationId"):
            if isinstance(obj.get(key), str) and obj[key]:
                conversation_id = obj[key]

        event = obj.get("event") or obj.get("type") or ""
        if event == "step_update" or isinstance(obj.get("step_update"), dict):
            su = obj.get("step_update") if isinstance(obj.get("step_update"), dict) else obj
            for k in ("conversation_id", "conversationId"):
                if isinstance(su.get(k), str) and su[k]:
                    conversation_id = su[k]
            delta = (
                su.get("text_delta")
                or su.get("textDelta")
                or su.get("delta")
                or obj.get("text_delta")
                or ""
            )
            if isinstance(delta, str) and delta:
                deltas.append(delta)
            elif isinstance(su.get("text"), str) and su["text"]:
                fallback_text = su["text"]

        elif event in ("assistant", "message"):
            msg_text = _extract_text_blocks(obj.get("message") or obj)
            if msg_text:
                fallback_text = msg_text

        elif event == "result" or isinstance(obj.get("result"), dict):
            res = obj.get("result") if isinstance(obj.get("result"), dict) else obj
            for k in ("conversation_id", "conversationId"):
                if isinstance(res.get(k), str) and res[k]:
                    conversation_id = res[k]

            status = str(res.get("status") or obj.get("status") or "").upper()
            err = res.get("error") or obj.get("error")
            if isinstance(err, dict):
                error_msg = str(err.get("message") or err)
            elif isinstance(err, str) and err:
                error_msg = err
            elif status == "ERROR" and not error_msg:
                error_msg = str(res.get("message") or res.get("text") or "agy returned ERROR")

            res_text = (
                _extract_text_blocks(res.get("text"))
                or _extract_text_blocks(res.get("response"))
                or _extract_text_blocks(res.get("output"))
                or _extract_text_blocks(res.get("message"))
            )
            if res_text:
                fallback_text = res_text

        elif event == "error":
            err = obj.get("error") or obj.get("message") or obj
            error_msg = str(err.get("message") if isinstance(err, dict) else err)

    text = "".join(deltas).strip() or fallback_text.strip() or "\n".join(raw_lines).strip()
    return text, conversation_id, error_msg


def _signal_process_group(proc: subprocess.Popen[str], sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except OSError:
        try:
            if sig == signal.SIGKILL:
                proc.kill()
            else:
                proc.terminate()
        except OSError:
            pass


def _terminate_process_group(proc: subprocess.Popen[str], *, wait_for_kill: bool = True) -> None:
    """Signal the entire process group started with `start_new_session=True`."""
    _signal_process_group(proc, signal.SIGTERM)

    def _escalate() -> None:
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            _signal_process_group(proc, signal.SIGKILL)

    if wait_for_kill:
        _escalate()
    else:
        threading.Thread(target=_escalate, daemon=True).start()


class AgyRunner:
    """Runs `agy` turns serialized per Telegram chat/thread with cancellation support."""

    def __init__(self, agy_bin: str = "agy") -> None:
        self.agy_bin = agy_bin
        self._meta_lock = threading.Lock()
        self._session_locks: dict[str, threading.Lock] = {}
        self._active_procs: dict[str, subprocess.Popen[str]] = {}
        self._cancelled_sessions: set[str] = set()

    def _get_lock(self, session_key: str) -> threading.Lock:
        with self._meta_lock:
            lock = self._session_locks.get(session_key)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_key] = lock
            return lock

    def is_running(self, session_key: str) -> bool:
        with self._meta_lock:
            proc = self._active_procs.get(session_key)
            return proc is not None and proc.poll() is None

    def cancel(self, session_key: str) -> bool:
        """Terminate an in-progress turn asynchronously. Returns True if signalled."""
        with self._meta_lock:
            proc = self._active_procs.get(session_key)
            if proc is None or proc.poll() is not None:
                return False
            self._cancelled_sessions.add(session_key)
        _terminate_process_group(proc, wait_for_kill=False)
        return True

    def run_turn(
        self,
        session_key: str,
        prompt: str,
        *,
        cwd: Path,
        conversation_id: str | None = None,
        execution_cfg: dict[str, Any] | None = None,
        session_cfg: dict[str, Any] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> AgyResult:
        """Execute a single `agy` turn for *session_key* and return `AgyResult`."""
        exec_cfg = execution_cfg or {}
        lock = self._get_lock(session_key)
        with lock:
            cmd = build_agy_command(
                prompt,
                conversation_id=conversation_id,
                execution_cfg=exec_cfg,
                session_cfg=session_cfg,
                agy_bin=self.agy_bin,
            )
            env = os.environ.copy()
            # Never leak TELEGRAM_BOT_TOKEN into the agent subprocess environment.
            env.pop("TELEGRAM_BOT_TOKEN", None)
            if extra_env:
                env.update(extra_env)

            turn_timeout_raw = exec_cfg.get("turnTimeoutSeconds", 600)
            turn_timeout = float(turn_timeout_raw) if turn_timeout_raw else 0.0
            timeout = turn_timeout if turn_timeout > 0 else None

            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
            with self._meta_lock:
                self._active_procs[session_key] = proc
                self._cancelled_sessions.discard(session_key)

            timed_out = False
            stdout_data = ""
            stderr_data = ""
            try:
                try:
                    stdout_data, stderr_data = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _terminate_process_group(proc, wait_for_kill=True)
                    try:
                        stdout_data, stderr_data = proc.communicate(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        for stream in (proc.stdout, proc.stderr):
                            if stream is not None:
                                try:
                                    stream.close()
                                except OSError:
                                    pass
                        _signal_process_group(proc, signal.SIGKILL)
                        try:
                            proc.wait(timeout=1.0)
                        except subprocess.TimeoutExpired:
                            pass
            finally:
                with self._meta_lock:
                    self._active_procs.pop(session_key, None)
                    was_cancelled = session_key in self._cancelled_sessions
                    self._cancelled_sessions.discard(session_key)

            text, new_conv_id, err_msg = parse_stream_json_lines(stdout_data.splitlines())
            if timed_out:
                err_msg = f"agy turn timed out after {int(turn_timeout)}s"
            elif proc.returncode not in (0, None) and not err_msg:
                stderr_clean = stderr_data.strip()
                err_msg = stderr_clean or f"agy exited with status {proc.returncode}"

            return AgyResult(
                text=text,
                conversation_id=new_conv_id or conversation_id,
                error=err_msg,
                returncode=124 if timed_out else (proc.returncode if proc.returncode is not None else 1),
                cancelled=was_cancelled,
                timed_out=timed_out,
            )
