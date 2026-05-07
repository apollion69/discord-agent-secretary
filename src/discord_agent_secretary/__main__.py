"""Allow `python -m discord_agent_secretary` to run the bot."""
from __future__ import annotations

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
