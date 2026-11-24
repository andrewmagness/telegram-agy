"""Pure-stdlib Markdown to Telegram HTML conversion and safe message chunking."""

from __future__ import annotations

import html
import re

MAX_TEXT_CHUNK = 4096

_FENCE_RE = re.compile(r"^```([A-Za-z0-9_+-]*)\s*$")
_FENCE_LINE_RE = re.compile(r"^\s*```([A-Za-z0-9_+-]*)\s*$")
_FENCE_CLOSE_RE = re.compile(r"^\s*```\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_HR_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
_UL_RE = re.compile(r"^(\s*)[-*+]\s+(.+)$")
_BQ_RE = re.compile(r"^\s*>\s?(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?(?:\s*:?-{2,}:?\s*\|)+\s*(?::?-{2,}:?\s*)?\|?\s*$")

_CODE_DOUBLE_RE = re.compile(r"``([^`\n]+?)``")
_CODE_SINGLE_RE = re.compile(r"`([^`\n]+?)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^()\s]+(?:\([^()\s]*\)[^()\s]*)*)\)")
_ALLOWED_LINK_SCHEMES = ("http://", "https://", "mailto:", "tg://")

_BOLD_ITALIC_RE = re.compile(r"\*\*\*(\S(?:[^*\n]*?\S)?)\*\*\*")
_BOLD_STAR_RE = re.compile(r"\*\*(\S(?:[^*\n]*?\S)?)\*\*")
_BOLD_UNDER_RE = re.compile(r"(?<![A-Za-z0-9_])__(\S(?:[^_\n]*?\S)?)__(?![A-Za-z0-9_])")
_STRIKE_RE = re.compile(r"~~(\S(?:[^~\n]*?\S)?)~~")
_ITALIC_STAR_RE = re.compile(r"\*(\S(?:[^*\n]*?\S)?)\*")
_ITALIC_UNDER_RE = re.compile(r"(?<![A-Za-z0-9_])_(\S(?:[^_\n]*?\S)?)_(?![A-Za-z0-9_])")
_PLACEHOLDER_RE = re.compile(r"\x00(CODE|LINK)(\d+)\x00")


def chunk_text(text: str, limit: int = MAX_TEXT_CHUNK) -> list[str]:
    """Split *text* into <= *limit*-char chunks, keeping fenced code blocks valid."""
    if not text:
        return [""]
    if limit < 16:
        limit = 16
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    open_fence_lang: str | None = None

    while remaining:
        prefix = f"```{open_fence_lang}\n" if open_fence_lang is not None else ""
        reserve_close = 4  # "\n```"
        effective_limit = max(8, limit - len(prefix) - reserve_close)

        if len(remaining) <= effective_limit:
            piece = prefix + remaining
            chunks.append(piece)
            break

        window = remaining[:effective_limit]
        split_at = -1
        for sep in ("\n\n", "\n", " "):
            idx = window.rfind(sep)
            if idx > effective_limit // 4:
                split_at = idx + len(sep)
                break
        if split_at <= 0:
            split_at = effective_limit

        raw_piece = remaining[:split_at]
        remaining = remaining[split_at:]

        state = open_fence_lang
        for line in raw_piece.splitlines():
            stripped = line.rstrip()
            if stripped.startswith("```"):
                if state is None:
                    match = _FENCE_RE.match(stripped)
                    state = match.group(1) if match else ""
                else:
                    state = None

        piece = prefix + raw_piece
        if state is not None:
            if not piece.endswith("\n"):
                piece += "\n"
            piece += "```"
        chunks.append(piece.rstrip("\n") if state is None else piece)
        open_fence_lang = state

    return [c for c in chunks if c]


def _format_emphasis(escaped_text: str) -> str:
    out = _BOLD_ITALIC_RE.sub(r"<b><i>\1</i></b>", escaped_text)
    out = _BOLD_STAR_RE.sub(r"<b>\1</b>", out)
    out = _BOLD_UNDER_RE.sub(r"<b>\1</b>", out)
    out = _STRIKE_RE.sub(r"<s>\1</s>", out)
    out = _ITALIC_STAR_RE.sub(r"<i>\1</i>", out)
    out = _ITALIC_UNDER_RE.sub(r"<i>\1</i>", out)
    return out


def _format_inline(text: str) -> str:
    """Convert inline Markdown in a single line or blockquote to Telegram HTML."""
    clean = text.replace("\x00", "")
    code_slots: list[str] = []
    link_slots: list[str] = []

    def _save_code(match: re.Match[str]) -> str:
        idx = len(code_slots)
        code_slots.append(f"<code>{html.escape(match.group(1), quote=False)}</code>")
        return f"\x00CODE{idx}\x00"

    clean = _CODE_DOUBLE_RE.sub(_save_code, clean)
    clean = _CODE_SINGLE_RE.sub(_save_code, clean)

    def _save_link(match: re.Match[str]) -> str:
        label = match.group(1)
        raw_url = match.group(2).strip()
        lower_url = raw_url.lower()
        rendered_label = _format_emphasis(html.escape(label, quote=False))
        idx = len(link_slots)
        if lower_url.startswith(_ALLOWED_LINK_SCHEMES):
            safe_href = html.escape(raw_url, quote=True)
            link_slots.append(f'<a href="{safe_href}">{rendered_label}</a>')
        elif lower_url.startswith("file://"):
            link_slots.append(f"<code>{html.escape(label, quote=False)}</code>")
        else:
            link_slots.append(rendered_label)
        return f"\x00LINK{idx}\x00"

    clean = _LINK_RE.sub(_save_link, clean)
    escaped = html.escape(clean, quote=False)
    formatted = _format_emphasis(escaped)

    def _restore(match: re.Match[str]) -> str:
        kind, idx_str = match.group(1), match.group(2)
        idx = int(idx_str)
        if kind == "CODE":
            return code_slots[idx]
        return _PLACEHOLDER_RE.sub(lambda m: code_slots[int(m.group(2))] if m.group(1) == "CODE" else m.group(0), link_slots[idx])

    # Two passes ensure CODE placeholders inside LINK labels also expand cleanly.
    formatted = _PLACEHOLDER_RE.sub(_restore, formatted)
    formatted = _PLACEHOLDER_RE.sub(_restore, formatted)
    return formatted


def _strip_inline_for_table(cell: str) -> str:
    s = cell.strip()
    s = _LINK_RE.sub(r"\1", s)
    s = re.sub(r"``([^`]+)``", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"__(.+?)__", r"\1", s)
    s = re.sub(r"~~(.+?)~~", r"\1", s)
    return s


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [_strip_inline_for_table(cell) for cell in stripped.split("|")]


def _render_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    col_count = max(len(r) for r in rows)
    normalized = [r + [""] * (col_count - len(r)) for r in rows]
    widths = [max(len(r[c]) for r in normalized) for c in range(col_count)]

    formatted_lines: list[str] = []
    for idx, row in enumerate(normalized):
        padded = " | ".join(row[c].ljust(widths[c]) for c in range(col_count)).rstrip()
        formatted_lines.append(padded)
        if idx == 0 and len(normalized) > 1:
            sep = "-+-".join("-" * w for w in widths)
            formatted_lines.append(sep)
    return f"<pre>{html.escape(chr(10).join(formatted_lines), quote=False)}</pre>"


def markdown_to_telegram_html(text: str) -> str:
    """Convert standard Markdown into Telegram Bot API HTML."""
    if not text:
        return ""

    lines = text.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        # Fenced code block
        fence_match = _FENCE_LINE_RE.match(line)
        if fence_match:
            lang = fence_match.group(1) or ""
            i += 1
            code_lines: list[str] = []
            while i < n and not _FENCE_CLOSE_RE.match(lines[i]):
                code_lines.append(lines[i])
                i += 1
            if i < n and _FENCE_CLOSE_RE.match(lines[i]):
                i += 1
            code_body = html.escape("\n".join(code_lines), quote=False)
            if lang:
                out.append(f'<pre><code class="language-{html.escape(lang, quote=True)}">{code_body}</code></pre>')
            else:
                out.append(f"<pre><code>{code_body}</code></pre>")
            continue

        # GFM pipe table (header row + separator row)
        if "|" in line and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):
            table_rows = [_split_table_row(line)]
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip() and not _FENCE_LINE_RE.match(lines[i]):
                table_rows.append(_split_table_row(lines[i]))
                i += 1
            out.append(_render_table(table_rows))
            continue

        # Blockquote
        bq_match = _BQ_RE.match(line)
        if bq_match:
            bq_lines: list[str] = []
            while i < n:
                m = _BQ_RE.match(lines[i])
                if not m:
                    break
                bq_lines.append(_format_inline(m.group(1)))
                i += 1
            out.append("<blockquote>" + "\n".join(bq_lines) + "</blockquote>")
            continue

        # Horizontal rule
        if _HR_RE.match(line):
            out.append("──────────")
            i += 1
            continue

        # ATX heading
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            out.append(f"<b>{_format_inline(heading_match.group(1))}</b>")
            i += 1
            continue

        # Unordered list item
        ul_match = _UL_RE.match(line)
        if ul_match:
            indent, item_body = ul_match.group(1), ul_match.group(2)
            out.append(f"{indent}• {_format_inline(item_body)}")
            i += 1
            continue

        out.append(_format_inline(line))
        i += 1

    return "\n".join(out)


def _split_chunk_recursive(md_chunk: str, html_limit: int, raw_limit: int) -> list[tuple[str, str]]:
    converted = markdown_to_telegram_html(md_chunk)
    if len(converted) <= html_limit or raw_limit <= 16:
        return [(converted, md_chunk)]
    smaller_raw = max(16, raw_limit // 2)
    results: list[tuple[str, str]] = []
    for sub in chunk_text(md_chunk, limit=smaller_raw):
        results.extend(_split_chunk_recursive(sub, html_limit, smaller_raw))
    return results


def chunk_markdown_for_telegram_html(
    text: str,
    limit: int = MAX_TEXT_CHUNK,
) -> list[tuple[str, str]]:
    """Split Markdown and convert to Telegram HTML, returning `(html_chunk, plain_md_chunk)` pairs."""
    if not text:
        return [("", "")]
    effective_limit = max(64, limit)
    initial_md_limit = min(effective_limit, max(64, int(effective_limit * 0.85)))
    pairs: list[tuple[str, str]] = []
    for md_chunk in chunk_text(text, limit=initial_md_limit):
        pairs.extend(_split_chunk_recursive(md_chunk, effective_limit, initial_md_limit))
    return pairs
