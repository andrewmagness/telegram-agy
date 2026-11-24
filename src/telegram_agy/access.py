"""Fail-closed pairing, allowlist, and group mention gating for Telegram."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any

PAIRING_TTL_SECONDS = 3600
MAX_PENDING_PAIRINGS = 3
VALID_DM_POLICIES = ("pairing", "allowlist", "disabled")


@dataclass
class AccessDecision:
    """Outcome of checking an inbound Telegram message against access.json."""

    allowed: bool
    reply_text: str | None = None
    reason: str = ""
    config_changed: bool = False


def prune_expired_pending(config: dict[str, Any], now: float | None = None) -> bool:
    """Remove expired pairing codes in-place. Returns True if any were removed."""
    now_ts = time.time() if now is None else now
    pending = config.setdefault("pending", {})
    expired = [
        code
        for code, entry in list(pending.items())
        if not isinstance(entry, dict)
        or (
            entry.get("expiresAt") is not None
            and float(entry["expiresAt"]) <= now_ts
        )
        or (
            entry.get("expiresAt") is None
            and float(entry.get("createdAt", 0)) + PAIRING_TTL_SECONDS <= now_ts
        )
    ]
    for code in expired:
        del pending[code]
    return bool(expired)


def _generate_pairing_code(pending: dict[str, Any]) -> str:
    for _ in range(16):
        code = secrets.token_hex(3).upper()
        if code not in pending:
            return code
    return secrets.token_hex(4).upper()


def _message_mentions_bot(
    message: dict[str, Any],
    bot_id: int | None,
    bot_username: str | None,
) -> bool:
    reply_to = message.get("reply_to_message")
    if isinstance(reply_to, dict) and bot_id is not None:
        reply_from = reply_to.get("from")
        if isinstance(reply_from, dict) and reply_from.get("id") == bot_id:
            return True

    if not bot_username:
        return False

    needle = f"@{bot_username.lower()}"
    for text_key, entities_key in (("text", "entities"), ("caption", "caption_entities")):
        body = message.get(text_key) or ""
        if not body:
            continue
        if needle in body.lower():
            return True
        for entity in message.get(entities_key) or []:
            if not isinstance(entity, dict):
                continue
            if entity.get("type") == "mention":
                offset = int(entity.get("offset", 0))
                length = int(entity.get("length", 0))
                mention_slice = body[offset : offset + length].lower()
                if mention_slice == needle:
                    return True
    return False


def evaluate_inbound_message(
    config: dict[str, Any],
    message: dict[str, Any],
    *,
    bot_id: int | None = None,
    bot_username: str | None = None,
    now: float | None = None,
) -> AccessDecision:
    """Evaluate whether an inbound Telegram message should reach agy."""
    now_ts = time.time() if now is None else now
    chat = message.get("chat") or {}
    sender = message.get("from") or {}

    # Ignore messages from bots (including self) or missing sender/chat IDs.
    if sender.get("is_bot"):
        return AccessDecision(allowed=False, reason="sender_is_bot")

    chat_id = chat.get("id")
    user_id = sender.get("id")
    if chat_id is None or user_id is None:
        return AccessDecision(allowed=False, reason="missing_ids")

    chat_type = chat.get("type", "")
    user_id_str = str(user_id)
    chat_id_str = str(chat_id)
    allow_from = {str(x) for x in config.get("allowFrom", [])}

    if chat_type == "private":
        policy = config.get("dmPolicy", "pairing")
        if policy == "disabled":
            return AccessDecision(allowed=False, reason="dm_disabled")
        if user_id_str in allow_from:
            return AccessDecision(allowed=True, reason="dm_allowlisted")
        if policy == "allowlist":
            return AccessDecision(allowed=False, reason="dm_not_allowlisted")

        # Default: pairing mode
        changed = prune_expired_pending(config, now=now_ts)
        pending = config.setdefault("pending", {})

        existing_code = None
        for code, entry in pending.items():
            if str(entry.get("userId")) == user_id_str:
                existing_code = code
                break

        if existing_code is not None:
            reply = (
                f"Pairing required. Your code is: {existing_code}\n\n"
                f"Approve it locally by running:\n"
                f"telegram-agy access pair {existing_code}"
            )
            return AccessDecision(
                allowed=False,
                reply_text=reply,
                reason="pairing_pending_reused",
                config_changed=changed,
            )

        if len(pending) >= MAX_PENDING_PAIRINGS:
            reply = (
                "Too many pending pairing requests right now. "
                "Please wait for an existing code to be approved or expire and try again."
            )
            return AccessDecision(
                allowed=False,
                reply_text=reply,
                reason="pairing_cap_reached",
                config_changed=changed,
            )

        code = _generate_pairing_code(pending)
        pending[code] = {
            "userId": user_id_str,
            "chatId": chat_id_str,
            "username": sender.get("username") or "",
            "firstName": sender.get("first_name") or "",
            "createdAt": now_ts,
            "expiresAt": now_ts + PAIRING_TTL_SECONDS,
        }
        reply = (
            f"Pairing required. Your code is: {code}\n\n"
            f"Approve it locally by running:\n"
            f"telegram-agy access pair {code}"
        )
        return AccessDecision(
            allowed=False,
            reply_text=reply,
            reason="pairing_issued",
            config_changed=True,
        )

    if chat_type in ("group", "supergroup"):
        groups = config.get("groups", {})
        group_cfg = groups.get(chat_id_str)
        if not isinstance(group_cfg, dict) or group_cfg.get("enabled") is False:
            return AccessDecision(allowed=False, reason="group_not_allowed")

        group_allow = [str(x) for x in group_cfg.get("allowFrom", [])]
        if group_allow and user_id_str not in group_allow:
            return AccessDecision(allowed=False, reason="group_sender_not_allowed")

        if group_cfg.get("requireMention", True):
            if not _message_mentions_bot(message, bot_id=bot_id, bot_username=bot_username):
                return AccessDecision(allowed=False, reason="group_mention_required")

        return AccessDecision(allowed=True, reason="group_allowed")

    return AccessDecision(allowed=False, reason="unsupported_chat_type")


def is_authorized_chat(config: dict[str, Any], chat_id: str | int) -> bool:
    """Return True if *chat_id* is an authorized DM user or allowed group."""
    chat_str = str(chat_id)
    allow_from = {str(x) for x in config.get("allowFrom", [])}
    if chat_str in allow_from:
        return True
    groups = config.get("groups", {})
    group_cfg = groups.get(chat_str)
    return isinstance(group_cfg, dict) and group_cfg.get("enabled") is not False


def is_authorized_callback_user(
    config: dict[str, Any],
    chat_id: str | int,
    user_id: str | int,
) -> bool:
    """Verify a user tapping an inline Approve/Deny button is authorized for *chat_id*."""
    chat_str = str(chat_id)
    user_str = str(user_id)
    allow_from = {str(x) for x in config.get("allowFrom", [])}
    if chat_str == user_str:
        return user_str in allow_from
    group_cfg = (config.get("groups") or {}).get(chat_str)
    if not isinstance(group_cfg, dict) or group_cfg.get("enabled") is False:
        return False
    group_allow = [str(x) for x in group_cfg.get("allowFrom", [])]
    if group_allow:
        return user_str in group_allow
    return user_str in allow_from


def approve_pairing(
    config: dict[str, Any],
    code: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Approve a pending 6-char pairing code and add the user to allowFrom."""
    now_ts = time.time() if now is None else now
    prune_expired_pending(config, now=now_ts)
    pending = config.setdefault("pending", {})
    normalized = code.strip().upper()
    if normalized not in pending:
        raise ValueError(f"Pairing code '{normalized}' not found or expired.")
    entry = pending.pop(normalized)
    user_id_str = str(entry["userId"])
    allow_from = [str(x) for x in config.setdefault("allowFrom", [])]
    if user_id_str not in allow_from:
        allow_from.append(user_id_str)
    config["allowFrom"] = allow_from
    return entry


def allow_user(config: dict[str, Any], user_id: str | int) -> bool:
    """Add a numeric Telegram user ID to allowFrom. Returns True if newly added."""
    user_str = str(user_id).strip()
    if not user_str.lstrip("-").isdigit():
        raise ValueError(f"Invalid numeric Telegram user ID: {user_id!r}")
    allow_from = [str(x) for x in config.setdefault("allowFrom", [])]
    if user_str in allow_from:
        return False
    allow_from.append(user_str)
    config["allowFrom"] = allow_from
    return True


def remove_user(config: dict[str, Any], user_id: str | int) -> bool:
    """Remove a Telegram user ID from allowFrom. Returns True if removed."""
    user_str = str(user_id).strip()
    allow_from = [str(x) for x in config.get("allowFrom", [])]
    if user_str not in allow_from:
        return False
    config["allowFrom"] = [x for x in allow_from if x != user_str]
    return True


def set_dm_policy(config: dict[str, Any], policy: str) -> None:
    """Set dmPolicy to pairing, allowlist, or disabled."""
    normalized = policy.strip().lower()
    if normalized not in VALID_DM_POLICIES:
        raise ValueError(
            f"Invalid dmPolicy {policy!r}; expected one of {', '.join(VALID_DM_POLICIES)}."
        )
    config["dmPolicy"] = normalized


def add_group(
    config: dict[str, Any],
    chat_id: str | int,
    *,
    require_mention: bool = True,
) -> None:
    """Allow a Telegram group/supergroup chat ID."""
    chat_str = str(chat_id).strip()
    if not chat_str.lstrip("-").isdigit():
        raise ValueError(f"Invalid numeric Telegram chat ID: {chat_id!r}")
    groups = config.setdefault("groups", {})
    existing = groups.get(chat_str) if isinstance(groups.get(chat_str), dict) else {}
    groups[chat_str] = {
        "enabled": True,
        "requireMention": require_mention,
        "allowFrom": list(existing.get("allowFrom", [])),
    }


def remove_group(config: dict[str, Any], chat_id: str | int) -> bool:
    """Remove a Telegram group/supergroup from groups. Returns True if removed."""
    chat_str = str(chat_id).strip()
    groups = config.get("groups", {})
    if chat_str not in groups:
        return False
    del groups[chat_str]
    return True


def allow_group_user(
    config: dict[str, Any],
    chat_id: str | int,
    user_id: str | int,
) -> bool:
    """Add a user ID to a group's allowFrom list (auto-registering the group if needed)."""
    chat_str = str(chat_id).strip()
    user_str = str(user_id).strip()
    if not user_str.lstrip("-").isdigit():
        raise ValueError(f"Invalid numeric Telegram user ID: {user_id!r}")
    groups = config.setdefault("groups", {})
    if chat_str not in groups or not isinstance(groups[chat_str], dict):
        add_group(config, chat_str)
    allow_list = [str(x) for x in groups[chat_str].setdefault("allowFrom", [])]
    if user_str in allow_list:
        return False
    allow_list.append(user_str)
    groups[chat_str]["allowFrom"] = allow_list
    return True
