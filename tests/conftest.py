from sinbosen_la548.client import Client
from sinbosen_la548.protocol import Frame, Opcode


def pytest_addoption(parser):
    group = parser.getgroup("e2e")
    group.addoption("--run-e2e", action="store_true", help="run browser E2E tests")
    group.addoption("--review-videos", action="store_true", help="slow browser E2E tests for review recordings")
    group.addoption("--e2e-artifacts", default="ignore/playwright", help="browser E2E artifact directory")


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: isolated Chromium UI end-to-end test")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-e2e"):
        return
    import pytest

    skip = pytest.mark.skip(reason="pass --run-e2e to launch browser E2E tests")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)


class FakeTransport:
    """Stands in for Transport in Client tests - no real socket."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, bytes, dict[str, object]]] = []
        self.responses: dict[object, bytes] = {}

    def request(self, opcode: object, payload: bytes = b"", *, timeout: float = 2.0, **kwargs: object) -> Frame:
        self.calls.append((opcode, payload, kwargs))
        return Frame(opcode=Opcode.WRITE_MODULE_ID, payload=self.responses.get(opcode, payload))


def client_with_fake_transport() -> tuple[Client, FakeTransport]:
    client = Client.__new__(Client)
    fake = FakeTransport()
    client._transport = fake  # type: ignore[assignment]
    return client, fake
