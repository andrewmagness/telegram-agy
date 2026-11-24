"""Command-line interface for telegram-agy."""

from __future__ import annotations

import argparse
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
    prune_expired_pending,
    remove_group,
    remove_user,
    set_dm_policy,
)
from .bridge import TelegramBridge
from .config import (
    get_state_dir,
    load_access_config,
    load_bot_token,
    save_access_config,
    save_bot_token,
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


def _read_pid(pid_file: Path) -> int | None:
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _cmd_configure(args: argparse.Namespace) -> int:
    state_dir = get_state_dir(args.state_dir)
    token = (args.token or "").strip()
    if not token:
        if sys.stdin.isatty():
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
    access_cfg = load_access_config(state_dir)
    if args.cwd:
        access_cfg.setdefault("execution", {})["defaultCwd"] = str(Path(args.cwd).expanduser().resolve())
    if args.permission_mode:
        exec_cfg = access_cfg.setdefault("execution", {})
        exec_cfg["permissionMode"] = args.permission_mode
        exec_cfg["sandbox"] = args.permission_mode != "full-auto"
    save_access_config(state_dir, access_cfg)

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
    cfg = load_access_config(state_dir)
    sub = args.access_cmd

    if sub == "show":
        if prune_expired_pending(cfg):
            save_access_config(state_dir, cfg)
        print(json.dumps(cfg, indent=2))
        return 0

    if sub == "pair":
        try:
            entry = approve_pairing(cfg, args.code)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        save_access_config(state_dir, cfg)
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

    if sub == "allow":
        added = allow_user(cfg, args.user_id)
        save_access_config(state_dir, cfg)
        print("User added to allowFrom." if added else "User already in allowFrom.")
        return 0

    if sub == "remove":
        removed = remove_user(cfg, args.user_id)
        save_access_config(state_dir, cfg)
        print("User removed from allowFrom." if removed else "User was not in allowFrom.")
        return 0

    if sub == "policy":
        set_dm_policy(cfg, args.policy)
        save_access_config(state_dir, cfg)
        print(f"dmPolicy set to {cfg['dmPolicy']}.")
        return 0

    if sub == "group":
        gsub = args.group_cmd
        if gsub == "add":
            add_group(cfg, args.chat_id, require_mention=not args.no_mention)
            save_access_config(state_dir, cfg)
            print(f"Group {args.chat_id} added (requireMention={not args.no_mention}).")
            return 0
        if gsub == "remove":
            removed = remove_group(cfg, args.chat_id)
            save_access_config(state_dir, cfg)
            print(f"Group {args.chat_id} removed." if removed else f"Group {args.chat_id} not found.")
            return 0
        if gsub == "allow":
            added = allow_group_user(cfg, args.chat_id, args.user_id)
            save_access_config(state_dir, cfg)
            print(
                f"User {args.user_id} added to group {args.chat_id}."
                if added
                else f"User {args.user_id} already allowed in group {args.chat_id}."
            )
            return 0

    if sub == "set":
        key = args.key
        raw_val = args.value
        exec_keys = {
            "defaultCwd",
            "permissionMode",
            "sandbox",
            "model",
            "mode",
            "approvalTimeoutSeconds",
        }
        top_keys = {"ackReaction", "textChunkLimit", "replyToMode"}
        if key in exec_keys:
            exec_cfg = cfg.setdefault("execution", {})
            if key == "sandbox":
                exec_cfg[key] = raw_val.lower() in ("1", "true", "yes", "on")
            elif key == "approvalTimeoutSeconds":
                exec_cfg[key] = int(raw_val)
            elif key == "permissionMode":
                if raw_val not in ("relay", "sandbox-auto", "full-auto"):
                    print("Error: permissionMode must be relay, sandbox-auto, or full-auto.", file=sys.stderr)
                    return 1
                exec_cfg[key] = raw_val
            elif key == "defaultCwd" and raw_val:
                exec_cfg[key] = str(Path(raw_val).expanduser().resolve())
            else:
                exec_cfg[key] = raw_val
        elif key in top_keys:
            if key == "textChunkLimit":
                cfg[key] = int(raw_val)
            else:
                cfg[key] = raw_val
        else:
            print(f"Error: Unknown config key {key!r}.", file=sys.stderr)
            return 1
        save_access_config(state_dir, cfg)
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
    bridge.run_forever()
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    state_dir = get_state_dir(args.state_dir)
    token = load_bot_token(state_dir)
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN not configured. Run `telegram-agy configure`.", file=sys.stderr)
        return 1

    pid_file = state_dir / "bridge.pid"
    existing_pid = _read_pid(pid_file)
    if existing_pid and _pid_is_alive(existing_pid):
        print(f"Bridge is already running (PID {existing_pid}).")
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
    running = bool(pid and _pid_is_alive(pid))
    token = load_bot_token(state_dir)
    sock_exists = (state_dir / "relay.sock").exists()
    cfg = load_access_config(state_dir)
    print(
        json.dumps(
            {
                "running": running,
                "pid": pid if running else None,
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


def _cmd_systemd(args: argparse.Namespace) -> int:
    state_dir = get_state_dir(args.state_dir)
    exe = shutil.which("telegram-agy")
    src_dir = str(Path(__file__).resolve().parent.parent)
    exec_start = (
        f"{exe} --state-dir {state_dir} run"
        if exe
        else f"{sys.executable} -m telegram_agy --state-dir {state_dir} run"
    )
    unit = f"""[Unit]
Description=Telegram Channel Bridge for Antigravity CLI (agy)
After=network-online.target

[Service]
Type=simple
Environment=PYTHONPATH={src_dir}
Environment=TELEGRAM_AGY_STATE_DIR={state_dir}
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
    p_cfg.add_argument("--token", default="", help="Telegram bot token from @BotFather")
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
    subparsers.add_parser("systemd", help="Print systemd --user unit file").set_defaults(
        func=_cmd_systemd
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
