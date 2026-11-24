"""Tests for pairing, allowlists, and group mention gating."""

from __future__ import annotations

import unittest

from telegram_agy.access import (
    MAX_PENDING_PAIRINGS,
    PAIRING_TTL_SECONDS,
    add_group,
    allow_group_user,
    allow_user,
    approve_pairing,
    clear_pending,
    evaluate_inbound_message,
    is_authorized_callback_user,
    is_authorized_chat,
    remove_user,
    set_dm_policy,
)
from telegram_agy.config import default_access_config


def _dm(user_id: int, text: str = "hello", username: str = "alice") -> dict:
    return {
        "message_id": 10,
        "date": 1700000000,
        "chat": {"id": user_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "username": username, "first_name": "Alice"},
        "text": text,
    }


def _group(
    chat_id: int,
    user_id: int,
    text: str = "hi",
    *,
    reply_from_id: int | None = None,
) -> dict:
    msg: dict = {
        "message_id": 20,
        "date": 1700000000,
        "chat": {"id": chat_id, "type": "supergroup", "title": "Devs"},
        "from": {"id": user_id, "is_bot": False, "username": "bob"},
        "text": text,
    }
    if reply_from_id is not None:
        msg["reply_to_message"] = {
            "message_id": 19,
            "from": {"id": reply_from_id, "is_bot": True},
            "text": "previous bot message",
        }
    return msg


class AccessTests(unittest.TestCase):
    def test_dm_pairing_flow_reuse_approve_and_clear(self) -> None:
        cfg = default_access_config()
        now = 1000.0
        d1 = evaluate_inbound_message(cfg, _dm(111), now=now)
        self.assertFalse(d1.allowed)
        self.assertTrue(d1.config_changed)
        self.assertIn("telegram-agy access pair", d1.reply_text or "")
        self.assertEqual(len(cfg["pending"]), 1)
        code = next(iter(cfg["pending"]))
        self.assertEqual(len(code), 6)

        d2 = evaluate_inbound_message(cfg, _dm(111, "again"), now=now + 10)
        self.assertFalse(d2.allowed)
        self.assertIn(code, d2.reply_text or "")
        self.assertEqual(len(cfg["pending"]), 1)

        entry = approve_pairing(cfg, code.lower(), now=now + 20)
        self.assertEqual(entry["userId"], "111")
        self.assertEqual(cfg["pending"], {})
        self.assertIn("111", cfg["allowFrom"])

        d3 = evaluate_inbound_message(cfg, _dm(111, "run tests"), now=now + 30)
        self.assertTrue(d3.allowed)
        self.assertIsNone(d3.reply_text)

        # clear_pending removes remaining pending codes
        evaluate_inbound_message(cfg, _dm(222), now=now + 40)
        self.assertEqual(clear_pending(cfg), 1)
        self.assertEqual(cfg["pending"], {})

    def test_pending_cap_and_expiry_pruning(self) -> None:
        cfg = default_access_config()
        now = 2000.0
        for idx in range(MAX_PENDING_PAIRINGS):
            d = evaluate_inbound_message(cfg, _dm(500 + idx), now=now)
            self.assertEqual(d.reason, "pairing_issued")

        d_cap = evaluate_inbound_message(cfg, _dm(999), now=now + 5)
        self.assertFalse(d_cap.allowed)
        self.assertEqual(d_cap.reason, "pairing_cap_reached")

        d_after = evaluate_inbound_message(
            cfg, _dm(999), now=now + PAIRING_TTL_SECONDS + 1
        )
        self.assertEqual(d_after.reason, "pairing_issued")
        self.assertEqual(len(cfg["pending"]), 1)

    def test_allowlist_and_disabled_policies(self) -> None:
        cfg = default_access_config()
        set_dm_policy(cfg, "allowlist")
        d = evaluate_inbound_message(cfg, _dm(111))
        self.assertFalse(d.allowed)
        self.assertIsNone(d.reply_text)

        allow_user(cfg, 111)
        self.assertTrue(evaluate_inbound_message(cfg, _dm(111)).allowed)

        set_dm_policy(cfg, "disabled")
        d_dis = evaluate_inbound_message(cfg, _dm(111))
        self.assertFalse(d_dis.allowed)
        self.assertEqual(d_dis.reason, "dm_disabled")

        remove_user(cfg, 111)
        self.assertNotIn("111", cfg["allowFrom"])

    def test_group_fail_closed_allowfrom_mention_boundaries_and_allow_all(self) -> None:
        cfg = default_access_config()
        allow_user(cfg, 100)  # global owner

        # Unregistered group -> dropped silently
        self.assertFalse(
            evaluate_inbound_message(
                cfg, _group(-1001, 100, "@my_bot hi"), bot_id=9, bot_username="my_bot"
            ).allowed
        )

        add_group(cfg, -1001, require_mention=True)
        # Empty group allowFrom falls back to global allowFrom -> stranger 222 rejected
        self.assertFalse(
            evaluate_inbound_message(
                cfg, _group(-1001, 222, "@my_bot hi"), bot_id=9, bot_username="my_bot"
            ).allowed
        )
        # Global owner 100 is allowed when mentioned
        self.assertTrue(
            evaluate_inbound_message(
                cfg, _group(-1001, 100, "hey @my_bot status?"), bot_id=9, bot_username="my_bot"
            ).allowed
        )

        # Loose substring mentions must NOT match
        self.assertFalse(
            evaluate_inbound_message(
                cfg, _group(-1001, 100, "check @my_bot_fans"), bot_id=9, bot_username="my_bot"
            ).allowed
        )
        self.assertFalse(
            evaluate_inbound_message(
                cfg, _group(-1001, 100, "email foo@my_bot.com"), bot_id=9, bot_username="my_bot"
            ).allowed
        )
        # Punctuation after mention is fine
        self.assertTrue(
            evaluate_inbound_message(
                cfg, _group(-1001, 100, "@my_bot, run tests"), bot_id=9, bot_username="my_bot"
            ).allowed
        )
        # Reply to bot -> allowed
        self.assertTrue(
            evaluate_inbound_message(
                cfg, _group(-1001, 100, "yes do it", reply_from_id=9), bot_id=9, bot_username="my_bot"
            ).allowed
        )

        # Explicit group allowFrom overrides fallback
        allow_group_user(cfg, -1001, 333)
        self.assertFalse(
            evaluate_inbound_message(
                cfg, _group(-1001, 100, "@my_bot"), bot_id=9, bot_username="my_bot"
            ).allowed
        )
        self.assertTrue(
            evaluate_inbound_message(
                cfg, _group(-1001, 333, "@my_bot"), bot_id=9, bot_username="my_bot"
            ).allowed
        )

        # Explicit allow_all_members=True permits anyone
        add_group(cfg, -1002, require_mention=False, allow_all_members=True)
        self.assertTrue(
            evaluate_inbound_message(
                cfg, _group(-1002, 777, "hello"), bot_id=9, bot_username="my_bot"
            ).allowed
        )

    def test_authorized_chat_and_callback_checks(self) -> None:
        cfg = default_access_config()
        allow_user(cfg, 42)
        add_group(cfg, -10099)
        self.assertTrue(is_authorized_chat(cfg, 42))
        self.assertTrue(is_authorized_chat(cfg, -10099))
        self.assertFalse(is_authorized_chat(cfg, 777))

        self.assertTrue(is_authorized_callback_user(cfg, 42, 42))
        self.assertFalse(is_authorized_callback_user(cfg, 42, 99))
        self.assertTrue(is_authorized_callback_user(cfg, -10099, 42))
        self.assertFalse(is_authorized_callback_user(cfg, -10099, 99))


if __name__ == "__main__":
    unittest.main()
