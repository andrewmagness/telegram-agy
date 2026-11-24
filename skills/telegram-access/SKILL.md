---
name: telegram-access
description: >-
  Manage Telegram channel pairing codes, user allowlists, DM policies, group
  chat rules, and execution settings for telegram-agy. Use when the user wants
  to pair a Telegram account, allow/remove users, change DM policy, or
  configure group access.
---

# Manage Telegram Channel Access (`telegram-agy`)

Use this skill to manage `~/.gemini/channels/telegram/access.json`, mirroring Claude Code's `/telegram:access` workflow.

Run `telegram-agy access ...` (or `PYTHONPATH="${PLUGIN_ROOT:-.}/src" python3 -m telegram_agy access ...` if not installed on `PATH`):

## Common Commands

- **Inspect current access policy, allowlist, pending codes, groups, and execution settings:**
  ```sh
  telegram-agy access show
  ```
- **Approve or clear pending DM pairing codes:**
  ```sh
  telegram-agy access pair <CODE>
  telegram-agy access clear-pending
  ```
- **Directly allow or remove a numeric Telegram user ID:**
  ```sh
  telegram-agy access allow <USER_ID>
  telegram-agy access remove <USER_ID>
  ```
- **Set DM access policy (`pairing` | `allowlist` | `disabled`):**
  ```sh
  telegram-agy access policy pairing
  telegram-agy access policy allowlist
  telegram-agy access policy disabled
  ```
- **Manage group chats (opt-in by `chat_id`, default `requireMention: true` and fail-closed to allowlisted users):**
  ```sh
  telegram-agy access group add <CHAT_ID>
  telegram-agy access group add <CHAT_ID> --no-mention
  telegram-agy access group add <CHAT_ID> --allow-all
  telegram-agy access group allow <CHAT_ID> <USER_ID>
  telegram-agy access group remove <CHAT_ID>
  ```
- **Update bridge/execution preferences:**
  ```sh
  telegram-agy access set permissionMode relay          # relay | sandbox-auto | full-auto
  telegram-agy access set sandbox true                  # true | false
  telegram-agy access set defaultCwd /path/to/project
  telegram-agy access set allowedRoots /path/one,/path/two
  telegram-agy access set safeTools read_file,list_dir,grep_search
  telegram-agy access set extraArgs ""
  telegram-agy access set model ""
  telegram-agy access set mode auto                     # auto | accept-edits | plan
  telegram-agy access set ackReaction 👀
  telegram-agy access set approvalTimeoutSeconds 120    # 5..120
  telegram-agy access set turnTimeoutSeconds 600        # 0..86400
  telegram-agy access set denyOversizedApprovals false  # true | false
  ```

Always run `telegram-agy access show` afterward to verify the updated configuration and report the current state concisely.
