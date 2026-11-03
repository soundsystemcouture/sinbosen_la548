"""Offline checks of hardware_test's loopback-interface configuration - no
audio device, no PipeWire and no LA548 (pactl is faked)."""

import subprocess

import pytest

from hardware_test import loopback
from hardware_test.loopback import LoopbackConfig, LoopbackConfigError, ensure_profile

REQUIRED = {"LA548_HIL_SINK": "test-sink", "LA548_HIL_SOURCE": "test-source"}


def test_defaults_need_only_the_sink_and_source():
    config = LoopbackConfig.from_env(REQUIRED)
    assert config == LoopbackConfig(
        sink="test-sink", source="test-source", playback_channels=("FL", "FR"), capture_channels=("FL", "FR"),
        sample_rate=48000, card=None, profile=None,
    )


def test_every_variable_is_read_and_whitespace_is_stripped():
    config = LoopbackConfig.from_env({
        "LA548_HIL_SINK": " test-sink ", "LA548_HIL_SOURCE": "test-source",
        "LA548_HIL_PLAYBACK_CHANNELS": " AUX0 , AUX1,AUX2 ", "LA548_HIL_CAPTURE_CHANNELS": "AUX2, AUX3",
        "LA548_HIL_RATE": " 96000 ", "LA548_HIL_CARD": "test-card", "LA548_HIL_PROFILE": "pro-audio",
    })
    assert config.sink == "test-sink"
    assert config.playback_channels == ("AUX0", "AUX1", "AUX2")
    assert config.capture_channels == ("AUX2", "AUX3")
    assert config.sample_rate == 96000
    assert (config.card, config.profile) == ("test-card", "pro-audio")


def test_empty_optional_variables_fall_back_to_the_defaults():
    config = LoopbackConfig.from_env({**REQUIRED, "LA548_HIL_PLAYBACK_CHANNELS": " ", "LA548_HIL_RATE": "",
                                      "LA548_HIL_CARD": "", "LA548_HIL_PROFILE": ""})
    assert config.playback_channels == ("FL", "FR")
    assert config.sample_rate == 48000
    assert config.card is None and config.profile is None


@pytest.mark.parametrize("value", ["FL", "AUX0,", "AUX0,,AUX1", "AUX0,AUX0"])
@pytest.mark.parametrize("name", ["LA548_HIL_PLAYBACK_CHANNELS", "LA548_HIL_CAPTURE_CHANNELS"])
def test_fewer_than_two_empty_or_repeated_channels_are_rejected(name, value):
    with pytest.raises(LoopbackConfigError, match=name):
        LoopbackConfig.from_env({**REQUIRED, name: value})


def test_one_error_names_every_missing_and_invalid_variable():
    with pytest.raises(LoopbackConfigError) as excinfo:
        LoopbackConfig.from_env({"LA548_HIL_SOURCE": "  ", "LA548_HIL_CAPTURE_CHANNELS": "FL",
                                 "LA548_HIL_RATE": "fast"})
    message = str(excinfo.value)
    for name in ("LA548_HIL_SINK", "LA548_HIL_SOURCE", "LA548_HIL_CAPTURE_CHANNELS", "LA548_HIL_RATE"):
        assert name in message
    assert "LA548_HIL_PLAYBACK_CHANNELS" not in message
    assert "hardware_test/README.md" in message


def test_the_reference_channel_is_optional_and_stripped():
    assert LoopbackConfig.from_env(REQUIRED).reference_channel is None
    config = LoopbackConfig.from_env({**REQUIRED, "LA548_HIL_CAPTURE_CHANNELS": "AUX2,AUX3",
                                      "LA548_HIL_REFERENCE_CHANNEL": " AUX0 "})
    assert config.reference_channel == "AUX0"


@pytest.mark.parametrize("reference", ["AUX2", "AUX3"])
def test_the_reference_channel_cannot_be_an_la548_output_capture(reference):
    with pytest.raises(LoopbackConfigError, match="LA548_HIL_REFERENCE_CHANNEL"):
        LoopbackConfig.from_env({**REQUIRED, "LA548_HIL_CAPTURE_CHANNELS": "AUX2,AUX3,AUX4",
                                 "LA548_HIL_REFERENCE_CHANNEL": reference})


def test_the_reference_channel_may_repeat_a_spare_capture_channel():
    config = LoopbackConfig.from_env({**REQUIRED, "LA548_HIL_CAPTURE_CHANNELS": "AUX2,AUX3,AUX4",
                                      "LA548_HIL_REFERENCE_CHANNEL": "AUX4"})
    assert config.reference_channel == "AUX4"


def test_the_error_is_a_value_error():
    with pytest.raises(ValueError, match="LA548_HIL_SINK"):
        LoopbackConfig.from_env({})


@pytest.mark.parametrize(("present", "missing"), [("LA548_HIL_CARD", "LA548_HIL_PROFILE"),
                                                  ("LA548_HIL_PROFILE", "LA548_HIL_CARD")])
def test_card_and_profile_must_be_set_together(present, missing):
    with pytest.raises(LoopbackConfigError, match=f"{missing} must be set together with {present}"):
        LoopbackConfig.from_env({**REQUIRED, present: "value"})


@pytest.mark.parametrize("rate", ["48000.0", "48k", "0", "-48000"])
def test_a_non_integer_or_non_positive_rate_is_rejected(rate):
    with pytest.raises(LoopbackConfigError, match="LA548_HIL_RATE"):
        LoopbackConfig.from_env({**REQUIRED, "LA548_HIL_RATE": rate})


def _cards_listing(active: str) -> str:
    return (
        "Card #40\n\tName: other-card\n\tActive Profile: off\n"
        f"Card #41\n\tName: test-card\n\tDriver: alsa\n\tActive Profile: {active}\n"
    )


class FakePactl:
    def __init__(self, active: str, *, switch_works: bool = True) -> None:
        self.active = active
        self.switch_works = switch_works
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if args[1:3] == ["list", "cards"]:
            return subprocess.CompletedProcess(args, 0, stdout=_cards_listing(self.active), stderr="")
        if args[1] == "set-card-profile" and self.switch_works:
            self.active = args[3]
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def _config(card: str | None = "test-card", profile: str | None = "pro-audio") -> LoopbackConfig:
    return LoopbackConfig(sink="test-sink", source="test-source", card=card, profile=profile)


def test_ensure_profile_is_a_no_op_without_a_card(monkeypatch):
    fake = FakePactl("off")
    monkeypatch.setattr(loopback.subprocess, "run", fake)
    ensure_profile(_config(card=None, profile=None))
    assert fake.calls == []


def test_ensure_profile_leaves_an_already_active_profile_alone(monkeypatch):
    fake = FakePactl("pro-audio")
    monkeypatch.setattr(loopback.subprocess, "run", fake)
    ensure_profile(_config())
    assert not any("set-card-profile" in call for call in fake.calls)


def test_ensure_profile_switches_and_checks_the_switch(monkeypatch):
    fake = FakePactl("output:analog-stereo")
    monkeypatch.setattr(loopback.subprocess, "run", fake)
    ensure_profile(_config())
    assert ["pactl", "set-card-profile", "test-card", "pro-audio"] in fake.calls
    assert fake.active == "pro-audio"


def test_ensure_profile_raises_when_the_switch_does_not_take(monkeypatch):
    monkeypatch.setattr(loopback.subprocess, "run", FakePactl("off", switch_works=False))
    with pytest.raises(RuntimeError, match="failed to switch"):
        ensure_profile(_config())


def test_ensure_profile_names_an_unknown_card(monkeypatch):
    monkeypatch.setattr(loopback.subprocess, "run", FakePactl("off"))
    with pytest.raises(LoopbackConfigError, match="LA548_HIL_CARD='missing-card'"):
        ensure_profile(_config(card="missing-card"))
