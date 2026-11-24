"""Tests for Markdown-to-Telegram-HTML conversion and safe chunking."""

from __future__ import annotations

import unittest

from telegram_agy.formatting import chunk_markdown_for_telegram_html, markdown_to_telegram_html


class FormattingTests(unittest.TestCase):
    def test_headings_emphasis_and_snake_case_preservation(self) -> None:
        md = (
            "# Title\n"
            "### Sub *Heading*\n"
            "Use **bold**, __also bold__, *italic*, _also italic_, ***both***, and ~~old~~.\n"
            "Identifier my_snake_case_var and foo_bar_baz must stay unitalicized."
        )
        out = markdown_to_telegram_html(md)
        self.assertIn("<b>Title</b>", out)
        self.assertIn("<b>Sub <i>Heading</i></b>", out)
        self.assertIn("<b>bold</b>", out)
        self.assertIn("<b>also bold</b>", out)
        self.assertIn("<i>italic</i>", out)
        self.assertIn("<i>also italic</i>", out)
        self.assertIn("<b><i>both</i></b>", out)
        self.assertIn("<s>old</s>", out)
        self.assertIn("my_snake_case_var", out)
        self.assertNotIn("<i>snake</i>", out)

    def test_inline_code_and_fenced_code_blocks(self) -> None:
        md = (
            "Run `echo *not italic* & <tag>`.\n\n"
            "```python\n"
            "def f(x):\n"
            "    return x < 10 and x > 0\n"
            "```"
        )
        out = markdown_to_telegram_html(md)
        self.assertIn("<code>echo *not italic* &amp; &lt;tag&gt;</code>", out)
        self.assertIn(
            '<pre><code class="language-python">def f(x):\n    return x &lt; 10 and x &gt; 0</code></pre>',
            out,
        )

    def test_unclosed_fence_and_no_language_fence(self) -> None:
        md = "```\nline <1>\n"
        out = markdown_to_telegram_html(md)
        self.assertEqual(out, "<pre><code>line &lt;1&gt;</code></pre>")

    def test_links_file_links_and_unsafe_schemes(self) -> None:
        md = (
            "See [Docs](https://example.com/some_page?a=1&b=2), "
            "[file.py](file:///home/user/file.py), "
            "and [bad](javascript:alert(1))."
        )
        out = markdown_to_telegram_html(md)
        self.assertIn('<a href="https://example.com/some_page?a=1&amp;b=2">Docs</a>', out)
        self.assertNotIn("<i>page", out)
        self.assertIn("<code>file.py</code>", out)
        self.assertNotIn("javascript:", out)
        self.assertIn("and bad.", out)

    def test_blockquotes_lists_rules_and_tables(self) -> None:
        md = (
            "> First **quoted** line\n"
            "> Second line\n\n"
            "- Item one\n"
            "  * Nested item\n"
            "---\n"
            "| Name | Status |\n"
            "| :--- | :--- |\n"
            "| **api** | `ok` |"
        )
        out = markdown_to_telegram_html(md)
        self.assertIn("<blockquote>First <b>quoted</b> line\nSecond line</blockquote>", out)
        self.assertIn("• Item one", out)
        self.assertIn("  • Nested item", out)
        self.assertIn("──────────", out)
        self.assertIn("<pre>Name | Status\n-----+-------\napi  | ok</pre>", out)

    def test_chunk_markdown_for_telegram_html_handles_html_expansion(self) -> None:
        # Dense '<' characters expand 4x in HTML ('&lt;')
        md = "<" * 90 + " " + ">" * 90
        pairs = chunk_markdown_for_telegram_html(md, limit=160)
        self.assertGreater(len(pairs), 1)
        for html_chunk, plain_chunk in pairs:
            self.assertLessEqual(len(html_chunk), 160)
            self.assertTrue(plain_chunk)


if __name__ == "__main__":
    unittest.main()
