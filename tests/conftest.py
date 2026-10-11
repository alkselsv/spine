from __future__ import annotations

import asyncio
import sys


if sys.platform == "win32":
    # psycopg's async driver requires SelectorEventLoop on Windows.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
