# telegram-agy

Bridge your Telegram bot to **Google Antigravity CLI (`agy`)** on your local machine—inspired by [Claude Code's Telegram channel plugin](https://github.com/anthropics/claude-plugins-official/tree/main/external_plugins/telegram) and built natively on Antigravity's plugin, skills, MCP, and `PreToolUse` hook architecture with **zero external runtime dependencies** (Python 3.10+ stdlib only).

## Features

- **Native Antigravity Plugin**: Installs cleanly with `agy plugin install` and adds:
  - **Skills**: `telegram-configure`, `telegram-access`, `telegram-bridge`
  - **MCP Server (`telegram-agy`)**: Exposes `telegram_reply`, `telegram_react`, `telegram_edit_message`, and `telegram_download_attachment`
  - **Channel Rules (`rules/AGENTS.md`)**: Teaches `agy` how to read `<channel source="telegram" ...>` envelopes and format mobile-friendly replies
  - **Permission Relay Hook (`hooks.json`)**: `PreToolUse` hook that sends inline `[Approve]` / `[Always (session)]` / `[Deny]` buttons to Telegram when guarded tools (`run_command`, `write_file`, `edit_file`) are invoked in `relay` mode
- **Fail-Closed Pairing & Access Control**:
  - Defaults to `dmPolicy: "pairing"`: unknown DM senders receive a 6-character pairing code (1h TTL, max 3 pending) and are blocked until approved locally
  - Supports `allowlist` and `disabled` DM policies, plus opt-in group chats with `requireMention` (`@bot` or reply-to-bot) and per-group `allowFrom`
- **Persistent Per-Chat `agy` Sessions**:
  - Tracks `conversationId` per chat/topic in `sessions.json` and resumes with `agy --conversation <id>`
  - Supports attachments (photos, documents, voice, audio, video, stickers saved to `~/.gemini/channels/telegram/inbox/`), emoji `ackReaction`, typing indicators, and code-block-aware 4096-char message chunking
  - Built-in Telegram chat commands: `/new`, `/status`, `/cd <path>`, `/model [name]`, `/mode [auto|accept-edits|plan]`, `/cancel`, `/help`

---

## Quickstart

### 1. Create a Telegram Bot
1. Message [@BotFather](https://t.me/BotFather) on Telegram and run `/newbot`.
2. Choose a name and a username ending in `bot`.
3. Copy the bot token (`<digits>:<secret>`).

### 2. Install the Plugin & CLI
Clone the repo (or install directly from GitHub once published):

```sh
git clone https://github.com/andrewmagness/telegram-agy.git
cd telegram-agy

# Install as an Antigravity plugin (adds skills, MCP tools, rules, and hook)
agy plugin install .

# Optional: install the CLI entrypoint globally
pip install -e .
```

You can also run directly without `pip install` via `PYTHONPATH=src python3 -m telegram_agy ...`.

### 3. Configure Your Bot Token
Either ask `agy` using the `telegram-configure` skill, or run:

```sh
telegram-agy configure --token "<YOUR_BOT_TOKEN>" --cwd /path/to/your/workspace
```

This validates the token against Telegram's `getMe` endpoint and saves it with `0600` permissions to `~/.gemini/channels/telegram/.env`.

### 4. Start the Bridge
```sh
telegram-agy start --cwd /path/to/your/workspace
```

Check status or logs anytime:
```sh
telegram-agy status
telegram-agy logs -n 50
```

### 5. Pair Your Telegram Account
1. Open Telegram and send any message to your bot.
2. The bot replies with a 6-character pairing code (e.g. `A1B2C3`).
3. Approve the code locally (or ask `agy` via the `telegram-access` skill):

```sh
telegram-agy access pair A1B2C3
```

You can now chat with `agy` directly from Telegram!

---

## Permission Modes

Configure how `agy` handles tool execution for Telegram-originated turns via `telegram-agy access set permissionMode <mode>`:

| Mode | Behavior |
| :--- | :--- |
| `relay` *(default)* | Runs `agy --sandbox` and intercepts guarded tools (`run_command`, `write_file`, `edit_file`) via `PreToolUse`, prompting you in Telegram with `[Approve]`, `[Always (session)]`, and `[Deny]` inline buttons. |
| `sandbox-auto` | Runs `agy --sandbox --dangerously-skip-permissions` (auto-approves within Antigravity's sandbox). |
| `full-auto` | Runs `agy --dangerously-skip-permissions` without sandbox restrictions (for trusted paired users only). |

Normal interactive `agy` sessions outside the Telegram bridge are unaffected because the `PreToolUse` hook passes through (`{}`) whenever `TELEGRAM_AGY_RELAY_SOCK` is unset.

---

## Telegram Commands

Once paired, send these commands in Telegram:

- `/new` — Start a fresh `agy` conversation for this chat
- `/status` — Show active workspace (`cwd`), `conversationId`, model, mode, and permission policy
- `/cd <path>` — Switch the active workspace directory for this chat (resets conversation)
- `/model [name]` — View or switch the `agy` model for this chat
- `/mode [auto|accept-edits|plan]` — View or switch the `agy` execution mode
- `/cancel` — Abort an in-progress `agy` turn
- `/help` — Show command help

---

## Access Management (`telegram-agy access`)

```sh
telegram-agy access show
telegram-agy access pair <CODE>
telegram-agy access allow <USER_ID>
telegram-agy access remove <USER_ID>
telegram-agy access policy <pairing|allowlist|disabled>
telegram-agy access group add <CHAT_ID> [--no-mention]
telegram-agy access group allow <CHAT_ID> <USER_ID>
telegram-agy access group remove <CHAT_ID>
telegram-agy access set <KEY> <VALUE>
```

---

## Architecture

```text
Telegram User
   │  (Bot API long-polling / inline callbacks)
   ▼
telegram-agy bridge daemon  ◄─── Unix socket (relay.sock) ───► PreToolUse Hook
   │                                                              ▲
   │  spawns per-turn `agy -p ... --output-format stream-json`    │
   ▼                                                              │
Antigravity CLI (agy) ──(stdio)──► telegram-agy MCP server ───────┘
                                   (telegram_reply, telegram_react,
                                    telegram_edit_message,
                                    telegram_download_attachment)
```

State is stored under `~/.gemini/channels/telegram/` (override with `TELEGRAM_AGY_STATE_DIR`):
- `.env` (`0600`) — `TELEGRAM_BOT_TOKEN`
- `access.json` (`0600`) — `dmPolicy`, `allowFrom`, `groups`, `pending`, `execution` settings
- `sessions.json` (`0600`) — per-chat `conversationId`, `cwd`, `model`, `mode`
- `inbox/` — downloaded Telegram attachments
- `bridge.pid`, `bridge.log`, `relay.sock` — daemon runtime files

---

## Running as a Background User Service (`systemd`)

```sh
telegram-agy systemd > ~/.config/systemd/user/telegram-agy.service
systemctl --user daemon-reload
systemctl --user enable --now telegram-agy.service
```

---

## Development & Tests

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
agy plugin validate --strict .
```

## License

[MIT](LICENSE)
