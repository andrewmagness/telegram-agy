"""Command-line interface for telegram-agy."""

from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .access import (
    add_group,
    allow_group_user,
    allow_user,
    approve_pairing,
    clear_pending,
    prune_expired_pending,
    remove_group,
    remove_user,
    set_dm_policy,
)
from .agy_runner import VALID_MODES, resolve_agy_bin
from .bridge import TelegramBridge
from .config import (
    DEFAULT_SAFE_TOOLS,
    get_state_dir,
    load_access_config,
    load_bot_token,
    save_bot_token,
    update_access_config,
)
from .mcp_server import TelegramMCPServer
from .permission_hook import run_hook
from .telegram_api import TelegramAPIError, TelegramClient


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_matches_telegram_agy(pid: int) -> bool:
    proc_dir = Path("/proc")
    if proc_dir.is_dir():
        cmdline_path = proc_dir / str(pid) / "cmdline"
        try:
            raw = cmdline_path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            return False
        return "telegram_agy" in raw or "telegram-agy" in raw

    # Non-Linux fallback (e.g., macOS/BSD): query ps and fail safe if unconfirmed.
    try:
        res = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
    except OSError:
        return False
    if res.returncode != 0:
        return False
    return "telegram_agy" in res.stdout or "telegram-agy" in res.stdout


def _bridge_lock_held(state_dir: Path) -> bool:
    lock_path = state_dir / "bridge.lock"
    if not lock_path.exists():
        return False
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _read_pid(pid_file: Path) -> int | None:
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _cmd_configure(args: argparse.Namespace) -> int:
    state_dir = get_state_dir(args.state_dir)
    if args.token_stdin:
        token = sys.stdin.read().strip()
    elif args.token:
        token = args.token.strip()
    elif os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
        token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    elif sys.stdin.isatty():
        token = getpass.getpass("Enter Telegram Bot Token from @BotFather: ").strip()
    else:
        token = sys.stdin.read().strip()

    if not token or ":" not in token:
        print("Error: A valid Telegram bot token (<digits>:<secret>) is required.", file=sys.stderr)
        return 1

    bot_info: dict[str, Any] = {}
    if not args.no_verify:
        try:
            bot_info = TelegramClient(token).get_me()
        except (TelegramAPIError, ValueError) as exc:
            print(f"Error verifying bot token with Telegram: {exc}", file=sys.stderr)
            return 1

    save_bot_token(state_dir, token)

    def _mutate(cfg: dict[str, Any]) -> None:
        if args.cwd:
            cfg.setdefault("execution", {})["defaultCwd"] = str(
                Path(args.cwd).expanduser().resolve()
            )
        if args.permission_mode:
            exec_cfg = cfg.setdefault("execution", {})
            exec_cfg["permissionMode"] = args.permission_mode
            exec_cfg["sandbox"] = args.permission_mode != "full-auto"

    update_access_config(state_dir, _mutate)

    username = bot_info.get("username")
    if username:
        print(f"Configured Telegram bot @{username} (id={bot_info.get('id')}).")
    else:
        print("Saved Telegram bot token (verification skipped).")
    print(f"State directory: {state_dir}")
    print("Next steps:")
    print("  1. Start the bridge: telegram-agy start")
    print("  2. Send a DM to your bot on Telegram to receive a pairing code")
    print("  3. Approve it: telegram-agy access pair <CODE>")
    return 0


def _cmd_access(args: argparse.Namespace) -> int:
    state_dir = get_state_dir(args.state_dir)
    sub = args.access_cmd

    if sub == "show":
        def _prune(cfg: dict[str, Any]) -> bool:
            return prune_expired_pending(cfg)

        cfg, _ = update_access_config(state_dir, _prune)
        print(json.dumps(cfg, indent=2))
        return 0

    if sub == "pair":
        try:
            _, entry = update_access_config(state_dir, lambda cfg: approve_pairing(cfg, args.code))
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(f"Paired Telegram user {entry['userId']} (@{entry.get('username') or 'unknown'}).")
        token = load_bot_token(state_dir)
        if token and entry.get("chatId"):
            try:
                TelegramClient(token).send_message(
                    entry["chatId"],
                    "✅ Paired! You can now send messages to agy from this chat.",
                )
            except Exception:
                pass
        return 0

    if sub == "clear-pending":
        _, count = update_access_config(state_dir, clear_pending)
        print(f"Cleared {count} pending pairing request(s).")
        return 0

    if sub == "allow":
        try:
            _, added = update_access_config(state_dir, lambda cfg: allow_user(cfg, args.user_id))
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print("User added to allowFrom." if added else "User already in allowFrom.")
        return 0

    if sub == "remove":
        try:
            _, removed = update_access_config(state_dir, lambda cfg: remove_user(cfg, args.user_id))
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print("User removed from allowFrom." if removed else "User was not in allowFrom.")
        return 0

    if sub == "policy":
        try:
            cfg, _ = update_access_config(state_dir, lambda c: set_dm_policy(c, args.policy))
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(f"dmPolicy set to {cfg['dmPolicy']}.")
        return 0

    if sub == "group":
        gsub = args.group_cmd
        if gsub == "add":
            try:
                update_access_config(
                    state_dir,
                    lambda c: add_group(
                        c,
                        args.chat_id,
                        require_mention=not args.no_mention,
                        allow_all_members=bool(args.allow_all),
                    ),
                )
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            print(
                f"Group {args.chat_id} added "
                f"(requireMention={not args.no_mention}, allowAllMembers={bool(args.allow_all)})."
            )
            return 0
        if gsub == "remove":
            try:
                _, removed = update_access_config(
                    state_dir, lambda c: remove_group(c, args.chat_id)
                )
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            print(f"Group {args.chat_id} removed." if removed else f"Group {args.chat_id} not found.")
            return 0
        if gsub == "allow":
            try:
                _, added = update_access_config(
                    state_dir, lambda c: allow_group_user(c, args.chat_id, args.user_id)
                )
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            print(
                f"User {args.user_id} added to group {args.chat_id}."
                if added
                else f"User {args.user_id} already allowed in group {args.chat_id}."
            )
            return 0

    if sub == "set":
        key = args.key
        raw_val = args.value

        def _apply_set(cfg: dict[str, Any]) -> None:
            exec_cfg = cfg.setdefault("execution", {})
            if key == "sandbox":
                exec_cfg[key] = raw_val.lower() in ("1", "true", "yes", "on")
            elif key == "denyOversizedApprovals":
                exec_cfg[key] = raw_val.lower() in ("1", "true", "yes", "on")
            elif key == "approvalTimeoutSeconds":
                val = int(raw_val)
                if not 5 <= val <= 120:
                    raise ValueError("approvalTimeoutSeconds must be between 5 and 120.")
                exec_cfg[key] = val
            elif key == "turnTimeoutSeconds":
                val = int(raw_val)
                if not 0 <= val <= 86400:
                    raise ValueError("turnTimeoutSeconds must be between 0 and 86400.")
                exec_cfg[key] = val
            elif key == "permissionMode":
                if raw_val not in ("relay", "sandbox-auto", "full-auto"):
                    raise ValueError("permissionMode must be relay, sandbox-auto, or full-auto.")
                exec_cfg[key] = raw_val
            elif key == "mode":
                if raw_val not in VALID_MODES:
                    raise ValueError(f"mode must be one of: {', '.join(VALID_MODES)}.")
                exec_cfg[key] = raw_val
            elif key == "model":
                if raw_val.startswith("-"):
                    raise ValueError("model cannot start with '-'.")
                exec_cfg[key] = raw_val
            elif key == "defaultCwd":
                exec_cfg[key] = str(Path(raw_val).expanduser().resolve()) if raw_val else ""
            elif key == "allowedRoots":
                roots = [
                    str(Path(p.strip()).expanduser().resolve())
                    for p in raw_val.split(",")
                    if p.strip()
                ]
                exec_cfg[key] = roots
            elif key == "safeTools":
                tools = [t.strip() for t in raw_val.split(",") if t.strip()]
                exec_cfg[key] = tools if tools else list(DEFAULT_SAFE_TOOLS)
            elif key == "extraArgs":
                exec_cfg[key] = [a.strip() for a in raw_val.split(",") if a.strip()]
            elif key == "textChunkLimit":
                val = int(raw_val)
                if not 64 <= val <= 4096:
                    raise ValueError("textChunkLimit must be between 64 and 4096.")
                cfg[key] = val
            elif key in ("ackReaction", "replyToMode"):
                cfg[key] = raw_val
            else:
                raise ValueError(f"Unknown config key {key!r}.")

        try:
            update_access_config(state_dir, _apply_set)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(f"Updated {key}.")
        return 0

    return 1


def _cmd_run(args: argparse.Namespace) -> int:
    state_dir = get_state_dir(args.state_dir)
    token = load_bot_token(state_dir)
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN not configured. Run `telegram-agy configure`.", file=sys.stderr)
        return 1
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else Path.cwd().resolve()
    bridge = TelegramBridge(state_dir, TelegramClient(token), default_cwd=cwd)
    try:
        bridge.run_forever()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    state_dir = get_state_dir(args.state_dir)
    token = load_bot_token(state_dir)
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN not configured. Run `telegram-agy configure`.", file=sys.stderr)
        return 1

    pid_file = state_dir / "bridge.pid"
    existing_pid = _read_pid(pid_file)
    if (existing_pid and _pid_is_alive(existing_pid) and _pid_matches_telegram_agy(existing_pid)) or _bridge_lock_held(state_dir):
        print(f"Bridge is already running (PID {existing_pid or 'locked'}).")
        return 0

    log_file = state_dir / "bridge.log"
    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else Path.cwd().resolve()
    env = os.environ.copy()
    env["TELEGRAM_AGY_STATE_DIR"] = str(state_dir)
    src_dir = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = f"{src_dir}:{env['PYTHONPATH']}" if env.get("PYTHONPATH") else src_dir

    cmd = [sys.executable, "-m", "telegram_agy", "--state-dir", str(state_dir), "run", "--cwd", str(cwd)]
    with open(log_file, "a", encoding="utf-8") as log_handle:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
        )
    pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")
    time.sleep(0.5)
    if not _pid_is_alive(proc.pid):
        print(f"Bridge exited immediately. Check {log_file}.", file=sys.stderr)
        return 1
    print(f"Started telegram-agy bridge (PID {proc.pid}). Logs: {log_file}")
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    state_dir = get_state_dir(args.state_dir)
    pid_file = state_dir / "bridge.pid"
    pid = _read_pid(pid_file)
    if not pid or not _pid_is_alive(pid):
        if pid_file.exists():
            pid_file.unlink()
        print("Bridge is not running.")
        return 0
    if not _pid_matches_telegram_agy(pid):
        print(
            f"Refusing to kill PID {pid}: process does not appear to be telegram-agy.",
            file=sys.stderr,
        )
        return 1
    os.kill(pid, 15)  # SIGTERM
    for _ in range(30):
        if not _pid_is_alive(pid):
            break
        time.sleep(0.1)
    if _pid_is_alive(pid):
        os.kill(pid, 9)
    if pid_file.exists():
        pid_file.unlink()
    print(f"Stopped telegram-agy bridge (PID {pid}).")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    state_dir = get_state_dir(args.state_dir)
    pid_file = state_dir / "bridge.pid"
    pid = _read_pid(pid_file)
    running = bool(
        (pid and _pid_is_alive(pid) and _pid_matches_telegram_agy(pid))
        or _bridge_lock_held(state_dir)
    )
    token = load_bot_token(state_dir)
    sock_exists = (state_dir / "relay.sock").exists()
    cfg = load_access_config(state_dir)
    print(
        json.dumps(
            {
                "running": running,
                "pid": pid if (pid and _pid_is_alive(pid)) else None,
                "stateDir": str(state_dir),
                "tokenConfigured": bool(token),
                "relaySocket": sock_exists,
                "dmPolicy": cfg.get("dmPolicy"),
                "allowedUsers": len(cfg.get("allowFrom") or []),
                "allowedGroups": len(cfg.get("groups") or {}),
                "pendingPairings": len(cfg.get("pending") or {}),
                "permissionMode": (cfg.get("execution") or {}).get("permissionMode"),
            },
            indent=2,
        )
    )
    return 0


def _cmd_logs(args: argparse.Namespace) -> int:
    state_dir = get_state_dir(args.state_dir)
    log_file = state_dir / "bridge.log"
    if not log_file.exists():
        print(f"No log file at {log_file}")
        return 0
    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-args.lines :]:
        print(line)
    return 0


def _systemd_quote(val: str) -> str:
    escaped = val.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def _build_service_path() -> str:
    home = Path.home()
    candidates: list[str] = []
    resolved_agy = resolve_agy_bin("agy")
    if os.path.isabs(resolved_agy):
        candidates.append(str(Path(resolved_agy).parent))
    candidates.extend(
        [
            str(home / ".local" / "bin"),
            str(home / ".gemini" / "antigravity" / "bin"),
            str(home / ".config" / "Antigravity" / "bin"),
        ]
    )
    candidates.extend(p for p in os.environ.get("PATH", "").split(os.pathsep) if p)
    candidates.extend(["/usr/local/bin", "/usr/bin", "/bin"])
    deduped: list[str] = []
    for entry in candidates:
        if entry not in deduped:
            deduped.append(entry)
    return os.pathsep.join(deduped)


def _cmd_systemd(args: argparse.Namespace) -> int:
    state_dir = get_state_dir(args.state_dir)
    cfg = load_access_config(state_dir)
    raw_cwd = args.cwd or (cfg.get("execution") or {}).get("defaultCwd") or str(Path.cwd())
    cwd = str(Path(raw_cwd).expanduser().resolve())
    exe = shutil.which("telegram-agy")
    src_dir = str(Path(__file__).resolve().parent.parent)
    service_path = _build_service_path()
    q_state = _systemd_quote(str(state_dir))
    q_cwd = _systemd_quote(cwd)
    if exe:
        exec_start = f"{_systemd_quote(exe)} --state-dir {q_state} run --cwd {q_cwd}"
    else:
        exec_start = (
            f"{_systemd_quote(sys.executable)} -m telegram_agy "
            f"--state-dir {q_state} run --cwd {q_cwd}"
        )
    unit = f"""[Unit]
Description=Telegram Channel Bridge for Antigravity CLI (agy)
After=network-online.target

[Service]
Type=simple
WorkingDirectory={cwd.replace("%", "%%")}
Environment={_systemd_quote(f'PATH={service_path}')}
Environment={_systemd_quote(f'PYTHONPATH={src_dir}')}
Environment={_systemd_quote(f'TELEGRAM_AGY_STATE_DIR={state_dir}')}
ExecStart={exec_start}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
    print(unit)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="telegram-agy", description="Telegram channel for agy")
    parser.add_argument("--state-dir", default=None, help="Override state directory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_cfg = subparsers.add_parser("configure", help="Configure Telegram bot token and defaults")
    p_cfg.add_argument("--token", default="", help="Telegram bot token (prefer --token-stdin or prompt)")
    p_cfg.add_argument("--token-stdin", action="store_true", help="Read bot token from stdin")
    p_cfg.add_argument("--cwd", default="", help="Default workspace directory")
    p_cfg.add_argument(
        "--permission-mode",
        choices=["relay", "sandbox-auto", "full-auto"],
        default="",
        help="Default permission mode",
    )
    p_cfg.add_argument("--no-verify", action="store_true", help="Skip Telegram getMe verification")
    p_cfg.set_defaults(func=_cmd_configure)

    p_acc = subparsers.add_parser("access", help="Manage pairing codes, allowlists, and policies")
    acc_sub = p_acc.add_subparsers(dest="access_cmd", required=True)
    acc_sub.add_parser("show", help="Show current access.json").set_defaults(func=_cmd_access)
    acc_sub.add_parser("clear-pending", help="Clear all pending pairing requests").set_defaults(
        func=_cmd_access
    )

    p_pair = acc_sub.add_parser("pair", help="Approve a 6-character pairing code")
    p_pair.add_argument("code")
    p_pair.set_defaults(func=_cmd_access)

    p_allow = acc_sub.add_parser("allow", help="Add numeric Telegram user ID to allowFrom")
    p_allow.add_argument("user_id")
    p_allow.set_defaults(func=_cmd_access)

    p_rem = acc_sub.add_parser("remove", help="Remove numeric Telegram user ID from allowFrom")
    p_rem.add_argument("user_id")
    p_rem.set_defaults(func=_cmd_access)

    p_pol = acc_sub.add_parser("policy", help="Set DM policy")
    p_pol.add_argument("policy", choices=["pairing", "allowlist", "disabled"])
    p_pol.set_defaults(func=_cmd_access)

    p_grp = acc_sub.add_parser("group", help="Manage allowed group chats")
    grp_sub = p_grp.add_subparsers(dest="group_cmd", required=True)
    p_gadd = grp_sub.add_parser("add", help="Allow a group chat ID")
    p_gadd.add_argument("chat_id")
    p_gadd.add_argument("--no-mention", action="store_true", help="Do not require @bot mention")
    p_gadd.add_argument(
        "--allow-all",
        action="store_true",
        help="Allow all members of this group (by default only allowlisted users are permitted)",
    )
    p_gadd.set_defaults(func=_cmd_access)
    p_grem = grp_sub.add_parser("remove", help="Remove a group chat ID")
    p_grem.add_argument("chat_id")
    p_grem.set_defaults(func=_cmd_access)
    p_galw = grp_sub.add_parser("allow", help="Allow specific user ID inside a group")
    p_galw.add_argument("chat_id")
    p_galw.add_argument("user_id")
    p_galw.set_defaults(func=_cmd_access)

    p_set = acc_sub.add_parser("set", help="Set an execution or display option")
    p_set.add_argument("key")
    p_set.add_argument("value")
    p_set.set_defaults(func=_cmd_access)

    p_run = subparsers.add_parser("run", help="Run the Telegram bridge in the foreground")
    p_run.add_argument("--cwd", default="", help="Default workspace directory")
    p_run.set_defaults(func=_cmd_run)

    p_start = subparsers.add_parser("start", help="Start the Telegram bridge daemon")
    p_start.add_argument("--cwd", default="", help="Default workspace directory")
    p_start.set_defaults(func=_cmd_start)

    subparsers.add_parser("stop", help="Stop the Telegram bridge daemon").set_defaults(func=_cmd_stop)
    subparsers.add_parser("status", help="Show bridge daemon status").set_defaults(func=_cmd_status)

    p_logs = subparsers.add_parser("logs", help="Show recent bridge logs")
    p_logs.add_argument("-n", "--lines", type=int, default=50)
    p_logs.set_defaults(func=_cmd_logs)

    subparsers.add_parser("mcp", help="Run the stdio MCP server").set_defaults(
        func=lambda a: TelegramMCPServer(
            state_dir=get_state_dir(a.state_dir, create=False)
        ).serve_stdio()
    )
    subparsers.add_parser("hook", help="Run the PreToolUse permission relay hook").set_defaults(
        func=lambda _a: run_hook()
    )

    p_sysd = subparsers.add_parser("systemd", help="Print systemd --user unit file")
    p_sysd.add_argument("--cwd", default="", help="Workspace directory for WorkingDirectory and --cwd")
    p_sysd.set_defaults(func=_cmd_systemd)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
