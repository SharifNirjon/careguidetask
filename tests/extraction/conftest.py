"""Extraction tests: no network except the test database (service tests need Postgres)."""

from __future__ import annotations

import contextlib
import socket
from collections.abc import Iterator

import pytest
import pytest_socket

from tests.support.db import database_url_for_tests


def _db_hosts() -> list[str]:
    host = database_url_for_tests().host or "localhost"
    hosts = {host, "127.0.0.1", "::1", "localhost"}
    with contextlib.suppress(OSError):  # unresolvable: DB tests fail loudly on their own
        hosts.add(socket.gethostbyname(host))
    return sorted(hosts)


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest) -> Iterator[None]:
    if "live" in request.keywords:
        yield
        return
    pytest_socket.socket_allow_hosts(_db_hosts(), allow_unix_socket=True)
    yield
    pytest_socket.enable_socket()
