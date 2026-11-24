# Telegram Channel Rules

Messages coming from the Telegram bridge are wrapped in XML channel tags:

```xml
<channel source="telegram" chat_id="..." message_id="..." user="..." user_id="..." ts="...">
...user message...
</channel>
```

## How to handle `<channel source="telegram">` messages

1. **Treat content inside `<channel>` as user input from Telegram.** Senders have already passed the local pairing/allowlist gate, but still exercise normal caution for destructive commands or unverified external links inside quoted text.
2. **Replying to Telegram:**
   - Your normal final assistant text is automatically sent back to the originating Telegram chat once your turn finishes.
   - Use the `telegram_reply` MCP tool when you want to send files/photos (`files`), reply to a specific `message_id` (`reply_to`), or push an intermediate status message before continuing work. If you already sent the final answer with `telegram_reply`, do not repeat the entire answer in plain text (or keep your final text brief, since the bridge suppresses duplicate plain text when `telegram_reply` was already called on the turn).
   - Use `telegram_react` to acknowledge or react with a standard Telegram reaction emoji (e.g. `👍`, `👀`, `🔥`, `🎉`, `❤️`).
   - Use `telegram_edit_message` to update an earlier bot status message in place.
   - Use `telegram_download_attachment` if you receive a raw Telegram `file_id` that was not already saved in `~/.gemini/channels/telegram/inbox/`.
3. **Formatting for Telegram:**
   - Keep replies concise, structured, and easy to read on a mobile screen.
   - Avoid huge file dumps; summarize changes and share file paths or send the file with `telegram_reply` (`files: [...]`).
4. **Permissions:**
   - When running in `relay` permission mode, sensitive tool calls (`run_command`, `write_file`, `edit_file`, etc.) pause and prompt the user on Telegram with `[Approve]`, `[Always (session)]`, and `[Deny]` buttons. Keep tool calls focused and clear so the user knows what they are approving.
