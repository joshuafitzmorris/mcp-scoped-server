import pytest

import scoped_mcp.tools  # noqa: F401  registers the real tools


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
