---
name: telegram-bridge
description: >-
  Start, stop, monitor, or run the Telegram-to-Antigravity bridge daemon and
  generate systemd service configuration. Use when the user wants to launch,
  restart, check status, or inspect logs for the Telegram bridge.
---

# Manage the Telegram ↔ Antigravity Bridge (`telegram-agy`)

Use this skill to run and manage the local daemon that polls Telegram Bot API updates and dispatches authorized messages to `agy`.

Run `telegram-agy ...` (or `PYTHONPATH="${PLUGIN_ROOT:-.}/src" python3 -m telegram_agy ...` if not installed on `PATH`):

## Operations

- **Start the bridge in the background:**
  ```sh
  telegram-agy start --cwd /path/to/workspace
  ```
- **Run in the foreground (for debugging or live logs):**
  ```sh
  telegram-agy run --cwd /path/to/workspace
  ```
- **Check bridge status (PID, uptime, relay socket, bot username):**
  ```sh
  telegram-agy status
  ```
- **View recent bridge logs:**
  ```sh
  telegram-agy logs -n 100
  ```
- **Stop the background bridge:**
  ```sh
  telegram-agy stop
  ```
- **Generate a `systemd --user` unit file for auto-start:**
  ```sh
  telegram-agy systemd
  ```

## Pre-flight Checklist

1. Verify `~/.gemini/channels/telegram/.env` exists (if missing, run the `telegram-configure` skill first).
2. Verify `agy` is on `PATH` and authenticated (`agy models`).
3. After starting, remind the user that any new DM sender will get a pairing code which can be approved with `telegram-agy access pair <CODE>` (or via the `telegram-access` skill).
