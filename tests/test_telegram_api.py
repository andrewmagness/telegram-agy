"""Tests for Telegram Bot API client, chunking, and attachment handling."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_agy.telegram_api import (
    MAX_DOWNLOAD_BYTES,
    TelegramAPIError,
    TelegramClient,
    chunk_text,
    sanitize_filename,
)


class TelegramApiTests(unittest.TestCase):
    def test_chunk_text_preserves_code_fences_and_limit(self) -> None:
        lines = ["```python"] + [f"x_{i} = {i} * 2" for i in range(120)] + ["```"]
        text = "\n".join(lines)
        chunks = chunk_text(text, limit=300)
        self.assertGreater(len(chunks), 2)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 300)
            self.assertEqual(chunk.count("```") % 2, 0, f"Unbalanced fence in chunk:\n{chunk}")
        self.assertTrue(chunks[0].startswith("```python\n"))
        self.assertTrue(chunks[1].startswith("```python\n"))

    def test_sanitize_filename_strips_traversal(self) -> None:
        self.assertEqual(sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_filename("my report (final).pdf"), "my_report_final_.pdf")
        self.assertEqual(sanitize_filename("..."), "attachment.bin")

    def test_send_message_parse_mode_fallback_on_400(self) -> None:
        client = TelegramClient("12345:secret")
        calls: list[tuple[str, dict]] = []

        def fake_call(method: str, payload=None, **_kwargs):
            calls.append((method, dict(payload or {})))
            if payload and "parse_mode" in payload:
                raise TelegramAPIError(method, 400, "Bad Request: can't parse entities")
            return {"message_id": 99}

        with patch.object(client, "call", side_effect=fake_call):
            res = client.send_message("42", "*unclosed", parse_mode="Markdown")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["message_id"], 99)
        self.assertEqual(len(calls), 2)
        self.assertIn("parse_mode", calls[0][1])
        self.assertNotIn("parse_mode", calls[1][1])

    def test_send_markdown_formats_html_and_falls_back_to_plain_markdown(self) -> None:
        client = TelegramClient("12345:secret")
        calls: list[dict] = []

        def fake_call(method: str, payload=None, **_kwargs):
            calls.append(dict(payload or {}))
            if payload and payload.get("parse_mode") == "HTML":
                raise TelegramAPIError(method, 400, "Bad Request: can't parse entities")
            return {"message_id": 100}

        with patch.object(client, "call", side_effect=fake_call):
            res = client.send_markdown("42", "**Hello** `world`")

        self.assertEqual(res[0]["message_id"], 100)
        self.assertEqual(calls[0]["parse_mode"], "HTML")
        self.assertEqual(calls[0]["text"], "<b>Hello</b> <code>world</code>")
        self.assertNotIn("parse_mode", calls[1])
        self.assertEqual(calls[1]["text"], "**Hello** `world`")

    def test_send_file_routes_photo_vs_document_multipart(self) -> None:
        client = TelegramClient("12345:secret")
        recorded: list[tuple[str, str, bytes]] = []

        def fake_execute(method: str, req, **_kwargs):
            recorded.append((method, req.get_header("Content-type"), req.data))
            return {"message_id": 101}

        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            img = Path(tmpdir) / "plot.png"
            doc = Path(tmpdir) / "notes.txt"
            img.write_bytes(b"\x89PNG\r\n\x1a\n")
            doc.write_text("hello", encoding="utf-8")

            with patch.object(client, "_execute", side_effect=fake_execute):
                client.send_file("42", img, caption="chart")
                client.send_file("42", doc, caption="notes")

        self.assertEqual(recorded[0][0], "sendPhoto")
        self.assertIn('name="photo"; filename="plot.png"', recorded[0][2].decode("utf-8", errors="ignore"))
        self.assertEqual(recorded[1][0], "sendDocument")
        self.assertIn('name="document"; filename="notes.txt"', recorded[1][2].decode("utf-8", errors="ignore"))

    def test_download_file_enforces_size_limit(self) -> None:
        client = TelegramClient("12345:secret")
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            with patch.object(
                client,
                "get_file",
                return_value={"file_path": "docs/big.bin", "file_size": MAX_DOWNLOAD_BYTES + 1},
            ):
                with self.assertRaises(ValueError):
                    client.download_file("fid_1", Path(tmpdir))


if __name__ == "__main__":
    unittest.main()
