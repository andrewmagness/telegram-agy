"""Configuration and atomic state persistence for telegram-agy."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

DEFAULT_STATE_DIR = Path("~/.gemini/channels/telegram")

DEFAULT_SAFE_TOOLS = [
    "read_file",
    "view_file",
    "view_file_outline",
    "view_code_item",
    "list_dir",
    "find_by_name",
    "grep_search",
    "update_focus",
    "update_steps",
    "list_resources",
    "read_resource",
    "telegram_react",
    "telegram_edit_message",
    "telegram_download_attachment",
]

_T = TypeVar("_T")


def default_access_config() -> dict[str, Any]:
    """Return a fresh default access.json configuration dictionary."""
    return {
        "dmPolicy": "pairing",
        "allowFrom": [],
        "groups": {},
        "pending": {},
        "ackReaction": "👀",
        "textChunkLimit": 4096,
        "replyToMode": "reply",
        "execution": {
            "defaultCwd": "",
            "permissionMode": "relay",
            "sandbox": True,
            "model": "",
            "mode": "auto",
            "extraArgs": [],
            "approvalTimeoutSeconds": 120,
            "turnTimeoutSeconds": 600,
            "allowedRoots": [],
            "denyOversizedApprovals": False,
            "safeTools": list(DEFAULT_SAFE_TOOLS),
        },
    }


def get_state_dir(override: str | Path | None = None, *, create: bool = True) -> Path:
    """Resolve the telegram-agy state directory (and create with 0700 perms if *create*)."""
    raw = override if override is not None else os.environ.get("TELEGRAM_AGY_STATE_DIR")
    path = Path(raw).expanduser() if raw else DEFAULT_STATE_DIR.expanduser()
    if create:
        existed = path.exists()
        path.mkdir(parents=True, exist_ok=True)
        if not existed:
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
    return path


def get_inbox_dir(state_dir: Path) -> Path:
    """Ensure and return the attachment inbox directory."""
    inbox = state_dir / "inbox"
    existed = inbox.exists()
    inbox.mkdir(parents=True, exist_ok=True)
    if not existed:
        try:
            os.chmod(inbox, 0o700)
        except OSError:
            pass
    return inbox


def atomic_write_text(path: Path, content: str, mode: int = 0o600) -> None:
    """Atomically write text to *path* with restrictive file permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.chmod(tmp_path, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: Any, mode: int = 0o600) -> None:
    """Atomically serialize *data* as formatted JSON to *path*."""
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=False) + "\n", mode=mode)


@contextmanager
def state_lock(state_dir: Path) -> Iterator[None]:
    """Acquire an advisory exclusive flock across CLI and bridge processes."""
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / ".state.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file without shell expansion."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            values[key] = val
    return values


def write_env_file(path: Path, updates: dict[str, str], mode: int = 0o600) -> None:
    """Update keys in a .env file atomically while preserving existing entries."""
    existing = read_env_file(path)
    existing.update(updates)
    lines = [f"{k}={v}" for k, v in existing.items()]
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""), mode=mode)


def load_bot_token(state_dir: Path) -> str:
    """Load TELEGRAM_BOT_TOKEN from environment or state_dir/.env."""
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if env_token:
        return env_token
    env_map = read_env_file(state_dir / ".env")
    return env_map.get("TELEGRAM_BOT_TOKEN", "").strip()


def save_bot_token(state_dir: Path, token: str) -> Path:
    """Persist TELEGRAM_BOT_TOKEN to state_dir/.env with mode 0600."""
    env_path = state_dir / ".env"
    with state_lock(state_dir):
        write_env_file(env_path, {"TELEGRAM_BOT_TOKEN": token.strip()}, mode=0o600)
    return env_path


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_access_config(state_dir: Path) -> dict[str, Any]:
    """Load access.json merged over defaults."""
    path = state_dir / "access.json"
    defaults = default_access_config()
    if not path.exists():
        return defaults
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
    if not isinstance(loaded, dict):
        return defaults
    return _deep_merge(defaults, loaded)


def save_access_config(state_dir: Path, config: dict[str, Any]) -> Path:
    """Atomically write access.json with mode 0600."""
    path = state_dir / "access.json"
    atomic_write_json(path, config, mode=0o600)
    return path


def update_access_config(
    state_dir: Path,
    mutator: Callable[[dict[str, Any]], _T],
    *,
    should_save: Callable[[_T], bool] | None = None,
) -> tuple[dict[str, Any], _T]:
    """Read, mutate, and optionally atomically write access.json under state_lock."""
    with state_lock(state_dir):
        cfg = load_access_config(state_dir)
        result = mutator(cfg)
        if should_save is None or should_save(result):
            save_access_config(state_dir, cfg)
        return cfg, result


def load_sessions(state_dir: Path) -> dict[str, dict[str, Any]]:
    """Load per-chat agy session metadata from sessions.json."""
    path = state_dir / "sessions.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_sessions(state_dir: Path, sessions: dict[str, dict[str, Any]]) -> Path:
    """Atomically write sessions.json with mode 0600."""
    path = state_dir / "sessions.json"
    atomic_write_json(path, sessions, mode=0o600)
    return path


def update_sessions(
    state_dir: Path,
    mutator: Callable[[dict[str, dict[str, Any]]], _T],
) -> tuple[dict[str, dict[str, Any]], _T]:
    """Read, mutate, and atomically write sessions.json under state_lock."""
    with state_lock(state_dir):
        sessions = load_sessions(state_dir)
        result = mutator(sessions)
        save_sessions(state_dir, sessions)
        return sessions, result


def load_offset(state_dir: Path) -> int | None:
    """Load the persisted Telegram getUpdates offset, if present."""
    path = state_dir / "offset.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and isinstance(data.get("offset"), int):
        return data["offset"]
    return None


def save_offset(state_dir: Path, offset: int) -> Path:
    """Persist the next Telegram getUpdates offset atomically."""
    path = state_dir / "offset.json"
    atomic_write_json(path, {"offset": int(offset)}, mode=0o600)
    return path
