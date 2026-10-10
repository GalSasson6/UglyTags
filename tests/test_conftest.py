import asyncio
from types import SimpleNamespace

import conftest
import pytest


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_loop_factories_return_usable_loops(monkeypatch, platform):
    monkeypatch.setattr(conftest, "sys", SimpleNamespace(platform=platform))
    factories = conftest.pytest_asyncio_loop_factories(None, None)

    assert isinstance(factories, dict)
    assert len(factories) == 1
    name, factory = next(iter(factories.items()))
    assert name
    assert callable(factory)
    if platform != "win32":
        assert factory is asyncio.new_event_loop

    loop = factory()
    try:
        assert isinstance(loop, asyncio.AbstractEventLoop)
        assert not loop.is_closed()
        if platform == "win32":
            assert isinstance(loop, asyncio.SelectorEventLoop)
        assert loop.run_until_complete(asyncio.sleep(0, result="ok")) == "ok"
    finally:
        loop.close()
