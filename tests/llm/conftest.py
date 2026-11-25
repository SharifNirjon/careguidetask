"""Gateway tests run with ALL sockets blocked: respx answers every HTTP call."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import pytest_socket
import respx

from tests.support.llm import BASE_URL, GatewayKit, make_kit


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest) -> Iterator[None]:
    if "live" in request.keywords:
        yield
        return
    pytest_socket.disable_socket(allow_unix_socket=True)
    yield
    pytest_socket.enable_socket()


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def kit() -> GatewayKit:
    return make_kit()
