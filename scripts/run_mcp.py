#!/usr/bin/env python3
"""Bootstrap entrypoint for running the telegram-agy MCP server from a plugin root."""

from __future__ import annotations

import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from telegram_agy.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["mcp", *sys.argv[1:]]))
