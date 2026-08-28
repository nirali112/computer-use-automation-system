"""Shared test configuration and fixtures.

Loads .env so `pytest` works on its own, and runs one instance of the mock
application for the whole session. The browser-backed tests need
CUA_CHROMIUM_PATH in environments whose preinstalled Chromium does not match
the pinned Playwright build; everything else runs regardless.
"""

import socket
import threading

import pytest
import uvicorn
from dotenv import load_dotenv

load_dotenv()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="session")
def base_url():
    """The mock core banking application, served for the whole test session."""
    from mockbank.app import app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        threading.Event().wait(0.05)
    assert server.started, "the mock application did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _no_armed_faults():
    """No test inherits another's armed fault."""
    from mockbank.faults import FAULTS

    FAULTS.reset()
    yield
    FAULTS.reset()
