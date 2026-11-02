"""Hardware-test gate: this whole directory is invisible to the default `uv run
pytest -q` gate (pyproject.toml's testpaths = ["tests"]), but even a deliberate
`pytest hardware_test` needs a *second*, independent opt-in here — both
`--run-hardware` and a real LA548_HOST env var — before anything touches the
real device. The loopback interface itself is configured by the LA548_HIL_*
variables (loopback.py), checked when each test connects its rig."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from .restore import restore_settings
from .rig import Rig


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-hardware",
        action="store_true",
        default=False,
        help=(
            "Actually talk to a real LA548 through a loopback audio interface. Requires LA548_HOST "
            "and the LA548_HIL_* variables (see hardware_test/README.md)."
        ),
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-hardware") and os.environ.get("LA548_HOST"):
        return
    skip_hardware = pytest.mark.skip(
        reason="needs --run-hardware, LA548_HOST=<device-ip> and the LA548_HIL_* loopback variables "
        "- see hardware_test/README.md"
    )
    for item in items:
        item.add_marker(skip_hardware)


@pytest.fixture(scope="session")
def la548_host() -> str:
    host = os.environ.get("LA548_HOST")
    if not host:
        pytest.fail("LA548_HOST is not set")
    return host


@pytest.fixture
def rig(la548_host: str) -> Iterator[Rig]:
    """A connected rig whose settings are restored to the pre-test snapshot
    afterwards, even when the test fails (see restore.py)."""
    with Rig.connect(la548_host) as r:
        baseline = r.client.read_settings()
        try:
            yield r
        finally:
            restore_settings(r.client, baseline)
