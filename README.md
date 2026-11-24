# telegram-agy

Bridge your Telegram bot to **Google Antigravity CLI (`agy`)** on your local machine—inspired by [Claude Code's Telegram channel plugin](https://github.com/anthropics/claude-plugins-official/tree/main/external_plugins/telegram) and built natively on Antigravity's plugin, skills, MCP, and `PreToolUse` hook architecture with **zero external runtime dependencies** (Python 3.10+ stdlib only).

## Features

- **Native Antigravity Plugin**: Installs cleanly with `agy plugin install` and adds:
  - **Skills**: `telegram-configure`, `telegram-access`, `telegram-bridge`
  - **MCP Server (`telegram-agy`)**: Exposes `telegram_reply`, `telegram_react`, `telegram_edit_message`, and `telegram_download_attachment` (pinned to the active turn's chat and restricted to workspace/inbox files)
  - **Channel Rules (`rules/AGENTS.md`)**: Teaches `agy` how to read `<channel source="telegram" ...>` envelopes and `<quoted_reply untrusted="true">` blocks
  - **Permission Relay Hook (`hooks.json`)**: `PreToolUse` hook that passes through (`allow`) in normal interactive `agy` sessions, and in Telegram bridge `relay` mode prompts via inline `[Approve]` / `[Always (session)]` / `[Deny]` buttons for any tool outside `safeTools`
- **Fail-Closed Pairing & Access Control**:
  - Defaults to `dmPolicy: "pairing"`: unknown DM senders receive a 6-character pairing code (1h TTL, max 3 pending) and are blocked until approved locally
  - Supports `allowlist` and `disabled` DM policies, plus opt-in group chats with `requireMention` (`@bot` or reply-to-bot) and fail-closed `allowFrom` (falling back to global `allowFrom` unless `--allow-all` is passed)
- **Persistent Per-Chat `agy` Sessions**:
  - Tracks `conversationId` per chat/topic in `sessions.json` and resumes with `agy --conversation=<id>`
  - Supports attachments (photos, documents, voice, audio, video, stickers saved to `~/.gemini/channels/telegram/inbox/`), emoji `ackReaction`, typing indicators, and code-block-aware 4096-char message chunking
  - Built-in Telegram chat commands: `/new`, `/status`, `/cd <path>`, `/model [name]`, `/mode [auto|accept-edits|plan]`, `/cancel`, `/help`

---

## Quickstart

### 1. Create a Telegram Bot
1. Message [@BotFather](https://t.me/BotFather) on Telegram and run `/newbot`.
2. Choose a name and a username ending in `bot`.
3. Copy the bot token (`<digits>:<secret>`).

### 2. Install the Plugin & CLI
Clone the repo (or install the plugin directly from GitHub):

```sh
git clone https://github.com/andrewmagness/telegram-agy.git
cd telegram-agy

# Install as an Antigravity plugin (adds skills, MCP tools, rules, and hook)
agy plugin install .
```

To install the `telegram-agy` CLI entrypoint:

- **Debian / Ubuntu (PEP 668 `externally-managed-environment` safe)**:
  ```sh
  # Option A: zero-dependency launcher script (no pip/pipx needed)
  mkdir -p ~/.local/bin
  printf '#!/usr/bin/env sh\nPYTHONPATH="%s/src${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m telegram_agy "$@"\n' "$PWD" > ~/.local/bin/telegram-agy
  chmod +x ~/.local/bin/telegram-agy

  # Option B: pipx
  pipx install -e .
  ```
- **Standard Python environments**:
  ```sh
  pip install -e .
  ```

### 3. Configure Your Bot Token
Run interactively (hidden prompt—keeps the token out of `ps` and shell history) or pipe via `--token-stdin`:

```sh
telegram-agy configure --cwd /path/to/your/workspace
```

*(Or pipe via stdin: `printf '%s\n' "$TELEGRAM_BOT_TOKEN" | telegram-agy configure --token-stdin --cwd /path/to/your/workspace`.)*

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

## Permission Modes & Safety Controls

Configure how `agy` handles tool execution for Telegram-originated turns via `telegram-agy access set permissionMode <mode>`:

| Mode | Behavior |
| :--- | :--- |
| `relay` *(default)* | Runs `agy --sandbox`. Read tools (`read_file`, `view_file`, `grep_search`, etc.) auto-approve **only** inside the active workspace or `inbox/`; reads outside those directories, `search_web`, `run_command`, file edits, subagents, unknown MCP tools, and `telegram_reply` with `files` prompt you in Telegram with `[Approve]` / `[Deny]` inline buttons. Access to `~/.gemini/channels/telegram/` outside `inbox/` is always denied. |
| `sandbox-auto` | Runs `agy --sandbox --dangerously-skip-permissions` (auto-approves within Antigravity's sandbox). Hook-level string matching against `~/.gemini/channels/telegram/` acts as defense-in-depth—use Antigravity's OS sandbox as the hard boundary. |
| `full-auto` | Runs `agy --dangerously-skip-permissions` without sandbox restrictions. Hook-level state-dir checks are only a speed bump here; use only for trusted single-owner setups. |

Additional hardening knobs (`telegram-agy access set <key> <value>`):
- `allowedRoots` — comma-separated list of directories `/cd` is confined to (empty = allow any directory for global owners)
- `safeTools` — comma-separated list of tools allowed without prompting in `relay` mode (empty resets to defaults)
- `extraArgs` — comma-separated extra CLI flags passed to `agy` (empty clears)
- `approvalTimeoutSeconds` — `5..120` seconds (default `120`)
- `turnTimeoutSeconds` — max runtime per `agy` turn in seconds (`0..86400`, default `600`)
- `denyOversizedApprovals` — `false` (default: attach full JSON arguments above 3800 chars before the prompt) or `true` (auto-deny oversized tool arguments)

Normal interactive `agy` sessions outside the Telegram bridge are unaffected because `PreToolUse` returns `{"decision": "allow"}` whenever `TELEGRAM_AGY_RELAY_SOCK` is unset.

---

## Telegram Commands

Once paired, send these commands in Telegram:

- `/new` — Start a fresh `agy` conversation for this chat
- `/status` — Show active workspace (`cwd`), `conversationId`, model, mode, and permission policy
- `/cd <path>` — Switch the active workspace directory for this chat (global owners only; resets conversation)
- `/model [name]` — View or switch the `agy` model for this chat
- `/mode [auto|accept-edits|plan]` — View or switch the `agy` execution mode
- `/cancel` — Abort an in-progress `agy` turn (and its child process group)
- `/help` — Show command help

---

## Access Management (`telegram-agy access`)

```sh
telegram-agy access show
telegram-agy access pair <CODE>
telegram-agy access clear-pending
telegram-agy access allow <USER_ID>
telegram-agy access remove <USER_ID>
telegram-agy access policy <pairing|allowlist|disabled>
telegram-agy access group add <CHAT_ID> [--no-mention] [--allow-all]
telegram-agy access group allow <CHAT_ID> <USER_ID>
telegram-agy access group remove <CHAT_ID>
telegram-agy access set <KEY> <VALUE>
```

---

## Running as a Background User Service (`systemd`)

```sh
telegram-agy systemd --cwd /path/to/your/workspace > ~/.config/systemd/user/telegram-agy.service
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
