---
name: telegram-configure
description: >-
  Configure the Telegram bot token, default workspace, and permission mode for
  telegram-agy. Use when the user wants to set up, connect, or reconfigure a
  Telegram bot with Antigravity CLI (agy).
---

# Configure Telegram Channel for Antigravity (`telegram-agy`)

Follow these steps to configure a Telegram bot for `agy` without ever echoing or committing the secret token.

## 1. Create a Telegram Bot (if the user doesn't have a token yet)

Guide the user to create a bot in Telegram:
1. Open Telegram and message `@BotFather`.
2. Send `/newbot` and follow the prompts to set a display name and a username ending in `bot` (for example `my_agy_bot`).
3. Copy the HTTP API bot token returned by `@BotFather` (format `<digits>:<secret>`).

## 2. Save and Verify Configuration

Locate the `telegram-agy` CLI (installed package `telegram-agy`, or fall back to running `PYTHONPATH="$PLUGIN_ROOT/src" python3 -m telegram_agy` from the plugin directory):

- **If the user provided a token in their message** (or via env var):
  Run without echoing the token back in chat:
  ```sh
  telegram-agy configure --token "<USER_TOKEN>"
  ```
  Or if running directly from the plugin directory:
  ```sh
  PYTHONPATH="${PLUGIN_ROOT:-.}/src" python3 -m telegram_agy configure --token "<USER_TOKEN>"
  ```
- **If running interactively**, omit `--token` so the user can paste it at a hidden prompt, or ask the user for the token.
- Optional flags:
  - `--cwd /path/to/workspace` — default working directory for `agy` turns triggered from Telegram
  - `--permission-mode relay|sandbox-auto|full-auto` — default `relay` (uses `--sandbox` + Telegram `[Approve]` / `[Deny]` inline buttons)

This writes `TELEGRAM_BOT_TOKEN` to `~/.gemini/channels/telegram/.env` (mode `0600`), verifies it via Telegram's `getMe` API, and initializes `~/.gemini/channels/telegram/access.json` (mode `0600`) with `dmPolicy: "pairing"`.

## 3. Verify and Next Steps

1. Confirm the bot `@username` returned by `configure` (never print the token).
2. Remind the user to:
   - Start the bridge using the `telegram-bridge` skill (`telegram-agy start` or `telegram-agy run`).
   - Send any DM to the bot on Telegram to receive a 6-character pairing code.
   - Approve that pairing code locally using the `telegram-access` skill (`telegram-agy access pair <CODE>`).
