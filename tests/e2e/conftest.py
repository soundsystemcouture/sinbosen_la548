"""Isolated Chromium test support for the Gradio application.

Every connection below is injected through ``FakeDeviceClient``.  The fake
factory only accepts ``e2e.fake.invalid`` so these tests cannot construct a
network client by accident.
"""

from __future__ import annotations

import html
import json
import re
import socket
import struct
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sinbosen_la548 import codecs
from sinbosen_la548.client import LockState
from sinbosen_la548.service import ControlKey, DSPService, controls, replace_control
from sinbosen_la548.settings import DeviceSettings, FIRSnapshot, decode_settings

FAKE_HOST = "e2e.fake.invalid"
VIEWPORT = {"width": 1920, "height": 1080}
WRITE_METHODS = frozenset({
    "set_mute", "set_phase", "set_volume", "set_delay", "set_mix_route", "set_eq_band", "set_xover",
    "set_dynamic_eq_band", "set_compressor", "set_limiter", "set_expander", "set_input_source",
    "set_freq_shifter", "set_fir", "save_program", "load_program", "channel_copy",
    "write_fir_coefs",
})


def expected_stream_close(text: str) -> bool:
    """Recognize the reviewed Gradio 6.27.0 SDK's deliberate close_stream abort only.

    In index-CRMvpSRm.js, s.onmessage calls xt exclusively for close_stream.
    That aborts its own completed SSE reader, which logs the cancellation as an
    error. Keep other aborts and future SDK bundles subject to normal failure.
    """
    asset = r"http://127\.0\.0\.1:\d+/assets/index-CRMvpSRm\.js:4:\d+"
    return (text.startswith("AbortError: signal is aborted without reason\n")
            and re.search(r"at xt \(" + asset + r"\)", text) is not None
            and re.search(r"at s\.onmessage \(" + asset + r"\)", text) is not None)


class FakeDeviceClient:
    """Stateful Client-shaped fake with an inspectable operation ledger."""

    def __init__(self, settings: DeviceSettings) -> None:
        self.settings = settings
        self.link_up = False
        self.locked = False
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.programs: dict[int, DeviceSettings] = {}
        impulse = struct.pack("<512f", 1.0, *([0.0] * 511))
        self.fir_coefficients = {(output, channel): impulse
                                 for output, count in ((False, settings.profile.input_count),
                                                       (True, settings.profile.output_count))
                                 for channel in range(count)}
        self._failures: dict[str, list[BaseException]] = {}
        self._verification_failures: dict[str, BaseException] = {}

    @property
    def writes(self) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
        return [call for call in self.calls if call[0] in WRITE_METHODS]

    def fail_next(self, method: str, error: BaseException) -> None:
        self._failures.setdefault(method, []).append(error)

    def fail_verification_after(self, write_method: str, error: BaseException) -> None:
        """Fail the final Apply readback after a named write has succeeded."""
        self._verification_failures[write_method] = error

    def external_edit(self, key: ControlKey, value: object) -> None:
        self.settings = replace_control(self.settings, key, value)

    def _call(self, method: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((method, args, kwargs))
        queued = self._failures.get(method, [])
        if queued:
            raise queued.pop(0)

    def connect(self) -> None:
        self._call("connect")
        self.link_up = True

    def close(self) -> None:
        self._call("close")
        self.link_up = False

    def read_lock_state(self) -> LockState:
        self._call("read_lock_state")
        return LockState(self.locked, "E2E fake is locked" if self.locked else "")

    def read_settings(self) -> DeviceSettings:
        self._call("read_settings")
        if not self.link_up:
            raise ConnectionError("E2E fake connection is down")
        return self.settings

    def _set(self, method: str, key: ControlKey, value: object, *args: Any, **kwargs: Any) -> None:
        self._call(method, *args, **kwargs)
        self.settings = replace_control(self.settings, key, value)
        error = self._verification_failures.pop(method, None)
        if error is not None:
            self.fail_next("read_settings", error)

    def set_mute(self, channel: int, value: bool, *, output: bool = False, **kwargs: Any) -> None:
        self._set("set_mute", ControlKey(output, channel, "mute"), value, channel, value, output=output, **kwargs)

    def set_phase(self, channel: int, value: bool, *, output: bool = False, **kwargs: Any) -> None:
        self._set("set_phase", ControlKey(output, channel, "polarity"), value, channel, value, output=output, **kwargs)

    def set_volume(self, channel: int, value: float, *, output: bool = False, **kwargs: Any) -> None:
        self._set("set_volume", ControlKey(output, channel, "gain"), value, channel, value, output=output, **kwargs)

    def set_delay(self, channel: int, value: float, *, output: bool = False, **kwargs: Any) -> None:
        self._set("set_delay", ControlKey(output, channel, "delay"), value, channel, value, output=output, **kwargs)

    def set_mix_route(self, input_id: int, output_id: int, value: bool, **kwargs: Any) -> None:
        self._set("set_mix_route", ControlKey(False, input_id, "route", output_id), value,
                  input_id, output_id, value, **kwargs)

    def set_eq_band(self, channel: int, index: int, *, output: bool = False, **kwargs: Any) -> None:
        key = ControlKey(output, channel, "eq", index)
        current = controls(self.settings)[key]
        self._set("set_eq_band", key, replace(current, **kwargs), channel, index, output=output, **kwargs)

    def set_xover(self, channel: int, kind: str, **kwargs: Any) -> None:
        key = ControlKey(True, channel, kind)
        current = controls(self.settings)[key]
        self._set("set_xover", key, replace(current, **kwargs), channel, kind, **kwargs)

    def _set_block(self, method: str, key: ControlKey, updates: dict[str, Any], *args: Any, **kwargs: Any) -> None:
        current = controls(self.settings)[key]
        self._set(method, key, replace(current, **updates), *args, **kwargs)

    def set_dynamic_eq_band(self, channel: int, index: int, **kwargs: Any) -> None:
        updates = {"q": kwargs.pop("qvalue"), **kwargs}
        self._set_block("set_dynamic_eq_band", ControlKey(False, channel, "dyneq", index), updates,
                        channel, index, **kwargs)

    def set_compressor(self, channel: int, *, output: bool = False, **kwargs: Any) -> None:
        self._set_block("set_compressor", ControlKey(output, channel, "compressor"), kwargs,
                        channel, output=output, **kwargs)

    def set_limiter(self, channel: int, **kwargs: Any) -> None:
        self._set_block("set_limiter", ControlKey(True, channel, "limiter"), kwargs, channel, **kwargs)

    def set_expander(self, channel: int, **kwargs: Any) -> None:
        self._set_block("set_expander", ControlKey(False, channel, "expander"), kwargs, channel, **kwargs)

    def set_input_source(self, channel: int, **kwargs: Any) -> None:
        self._set_block("set_input_source", ControlKey(False, channel, "input_source"), kwargs, channel, **kwargs)

    def set_freq_shifter(self, channel: int, *, freq_level: int, **kwargs: Any) -> None:
        self._set_block("set_freq_shifter", ControlKey(False, channel, "frequency_shifter"),
                        {"level": freq_level, **kwargs}, channel, freq_level=freq_level, **kwargs)

    def set_fir(self, channel: int, *, output: bool = False, **kwargs: Any) -> None:
        snapshot = FIRSnapshot.from_bytes(codecs.fir_struct.encode((
            kwargs["enabled"], kwargs["fir_type"], kwargs["win_type"], kwargs["freq_low_hz"],
            kwargs["freq_high_hz"], kwargs["attenuation_db"], kwargs["order_index"],
        )))
        self._set("set_fir", ControlKey(output, channel, "fir"), snapshot, channel, output=output, **kwargs)

    def read_fir_coefs(self, channel: int, *, output: bool = False) -> bytes:
        self._call("read_fir_coefs", channel, output=output)
        return self.fir_coefficients[output, channel]

    def write_fir_coefs(self, channel: int, coefficients: bytes, *, output: bool = False) -> None:
        self._call("write_fir_coefs", channel, coefficients, output=output)
        self.fir_coefficients[output, channel] = coefficients

    def save_program(self, slot: int) -> None:
        self._call("save_program", slot)
        self.programs[slot] = self.settings

    def load_program(self, slot: int) -> None:
        self._call("load_program", slot)
        self.settings = self.programs[slot]

    def channel_copy(self, source: int, targets: tuple[int, ...], count: int, *, output: bool = False) -> None:
        if len(targets) != 1:
            raise ValueError("channel_copy requires exactly one target, matching the manufacturer's app")
        self._call("channel_copy", source, targets, count, output=output)
        channels = list(self.settings.outputs if output else self.settings.inputs)
        for target in targets:
            old = channels[target]
            channels[target] = replace(channels[source], channel_id=target, name=old.name, name_raw=old.name_raw)
        self.settings = replace(self.settings, outputs=tuple(channels)) if output else replace(
            self.settings, inputs=tuple(channels))


def _fixture_settings() -> DeviceSettings:
    source = Path(__file__).parents[1] / "fixtures" / "read_group_sample.bin"
    settings = decode_settings(source.read_bytes())
    return replace(settings, mix_matrix=tuple(tuple(True for _ in row) for row in settings.mix_matrix))


class FakeFactory:
    def __init__(self, client: FakeDeviceClient) -> None:
        self.client = client
        self.hosts: list[str] = []

    def __call__(self, host: str) -> FakeDeviceClient:
        assert host == FAKE_HOST, f"E2E must use {FAKE_HOST!r}, got {host!r}"
        self.hosts.append(host)
        return self.client


class E2ERun:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.rows: list[dict[str, str]] = []

    def add(self, *, scenario: str, passed: bool, description: str) -> None:
        self.rows.append({"scenario": scenario, "passed": "passed" if passed else "failed", "description": description})

    def write_index(self) -> None:
        rows = "\n".join(
            "<tr><td>{}</td><td class=\"{}\">{}</td><td>{}</td><td><a href=\"{}/{}.webm\">video</a> · "
            "<a href=\"{}/trace.zip\">trace</a> · <a href=\"{}/final.png\">screenshot</a> · "
            "<a href=\"{}/client-events.json\">client events</a></td></tr>".format(
                html.escape(row["scenario"]), row["passed"], row["passed"], html.escape(row["description"]),
                row["scenario"], row["scenario"], row["scenario"], row["scenario"], row["scenario"],
            ) for row in self.rows
        )
        (self.root / "index.html").write_text(
            "<!doctype html><meta charset=utf-8><title>LA548 Playwright review</title>"
            "<style>body{font:16px system-ui;margin:2rem}table{border-collapse:collapse}td,th{padding:.6rem;"
            "border:1px solid #bbb}.passed{color:#087f23}.failed{color:#bd1e1e}</style>"
            "<h1>LA548 Playwright review</h1><p>Each scenario uses the injected fake device only.</p>"
            "<table><tr><th>Scenario</th><th>Result</th><th>Coverage</th><th>Artifacts</th></tr>" + rows + "</table>")


@pytest.fixture(scope="session")
def e2e_run(pytestconfig: pytest.Config) -> E2ERun:
    base = Path(str(pytestconfig.getoption("--e2e-artifacts")))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    root = base / f"{stamp}-{Path.cwd().name}"
    suffix = 1
    while root.exists():
        suffix += 1
        root = base / f"{stamp}-{Path.cwd().name}-{suffix}"
    root.mkdir(parents=True)
    run = E2ERun(root)
    yield run
    run.write_index()
    print(f"\nPlaywright review artifacts: {run.root / 'index.html'}")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Any:
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


class Scenario:
    def __init__(self, *, page: Any, app: Any, service: DSPService, device: FakeDeviceClient, factory: FakeFactory,
                 directory: Path, review: bool, errors: list[str]) -> None:
        self.page = page
        self.app = app
        self.service = service
        self.device = device
        self.factory = factory
        self.directory = directory
        self.review = review
        self.errors = errors

    def connect(self) -> None:
        self.page.get_by_label("DSP IP address", exact=True).fill(FAKE_HOST)
        self.page.get_by_role("button", name="Connect", exact=True).click()
        self.page.locator("#status").get_by_text("Connected", exact=False).wait_for(timeout=20000)
        assert self.factory.hosts == [FAKE_HOST]

    def status(self, text: str) -> None:
        self.page.locator("#status").get_by_text(re.compile(text, re.I)).wait_for(timeout=20000)

    def reveal(self, locator: Any, name: str) -> None:
        locator.scroll_into_view_if_needed(timeout=10000)
        self.page.screenshot(path=str(self.directory / f"{name}.png"), full_page=False)
        if self.review:
            self.page.wait_for_timeout(700)

    def assert_clean_browser(self) -> None:
        assert not self.errors, "Unexpected browser JavaScript errors:\n" + "\n".join(self.errors)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def e2e(request: pytest.FixtureRequest, e2e_run: E2ERun) -> Scenario:
    pytest.importorskip("playwright.sync_api", reason="install the e2e dependency group")
    from playwright.sync_api import sync_playwright

    from sinbosen_la548.ui import CSS, create_app

    scenario_name = re.sub(r"^test_", "", request.node.name)
    directory = e2e_run.root / scenario_name
    directory.mkdir()
    downloads = directory / "downloads"
    downloads.mkdir()
    device = FakeDeviceClient(_fixture_settings())
    factory = FakeFactory(device)
    service = DSPService(device.settings, client_factory=factory)
    app = create_app(service=service)
    port = _free_port()
    errors: list[str] = []
    client_events: list[dict[str, str]] = []
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(viewport=VIEWPORT, record_video_dir=str(directory), record_video_size=VIEWPORT,
                                  accept_downloads=True)
    context.tracing.start(screenshots=True, snapshots=True, sources=True)
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))
    def console_message(message: Any) -> None:
        if message.type != "error":
            return
        expected = expected_stream_close(message.text)
        client_events.append({"classification": "expected-close-stream" if expected else "unexpected",
                              "text": message.text})
        if not expected:
            errors.append(f"console {message.type}: {message.text}")
    page.on("console", console_message)
    launched = False
    try:
        app.launch(server_name="127.0.0.1", server_port=port, share=False, inbrowser=False, quiet=True,
                   prevent_thread_lock=True, footer_links=[], css=CSS)
        launched = True
        page.goto(f"http://127.0.0.1:{port}", wait_until="domcontentloaded")
        page.locator("#status").get_by_text("Offline demo", exact=False).wait_for(timeout=30000)
        page.locator("#response-main .js-plotly-plot").wait_for(timeout=30000)
        yield Scenario(page=page, app=app, service=service, device=device, factory=factory, directory=directory,
                       review=bool(request.config.getoption("--review-videos")), errors=errors)
    finally:
        (directory / "client-events.json").write_text(json.dumps(client_events, indent=2))
        try:
            page.screenshot(path=str(directory / "final.png"), full_page=True)
        except Exception:
            pass
        try:
            context.tracing.stop(path=str(directory / "trace.zip"))
        finally:
            context.close()
            video = page.video.path()
            if video is not None:
                Path(video).rename(directory / f"{scenario_name}.webm")
            browser.close()
            playwright.stop()
            if launched:
                app.close()
            service.close()
            passed = bool(getattr(request.node, "rep_call", None) and request.node.rep_call.passed)
            e2e_run.add(scenario=scenario_name, passed=passed, description=request.node.function.__doc__ or "")
            if errors and passed:
                pytest.fail("Unexpected browser JavaScript errors:\n" + "\n".join(errors), pytrace=False)
