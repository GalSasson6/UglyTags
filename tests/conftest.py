import asyncio
import selectors
import sys


def pytest_asyncio_loop_factories(config, item):
    if sys.platform == "win32":
        # psycopg's async mode cannot use the default Windows Proactor loop.
        return {"selector": lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())}
    return None
