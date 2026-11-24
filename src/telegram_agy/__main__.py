"""Module entrypoint for `python3 -m telegram_agy`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
