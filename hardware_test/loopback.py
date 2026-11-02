"""Which audio interface the HIL rig plays through and records from.

The rig plays test signals out an audio interface into LA548 inputs A/B and
records LA548 outputs 1/2 back through the same interface. Which PipeWire
nodes and channels that means depends on the host, so it comes from the
environment (see hardware_test/README.md for how to find the names):

- `LA548_HIL_SINK` (required): PipeWire playback node name.
- `LA548_HIL_SOURCE` (required): PipeWire capture node name.
- `LA548_HIL_PLAYBACK_CHANNELS` (default `FL,FR`): comma-separated
  channel-map names, at least two; the first two drive LA548 inputs A/B and
  any others play silence.
- `LA548_HIL_CAPTURE_CHANNELS` (default `FL,FR`): at least two; the first
  two record LA548 outputs 1/2.
- `LA548_HIL_RATE` (default 48000): sample rate for playback and capture.
- `LA548_HIL_CARD` and `LA548_HIL_PROFILE` (optional, both or neither): a
  pactl card profile to select before running. Many USB interfaces expose
  flat, correctly ordered `AUXn` channels only under the `pro-audio` profile.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

SINK_ENV = "LA548_HIL_SINK"
SOURCE_ENV = "LA548_HIL_SOURCE"
PLAYBACK_CHANNELS_ENV = "LA548_HIL_PLAYBACK_CHANNELS"
CAPTURE_CHANNELS_ENV = "LA548_HIL_CAPTURE_CHANNELS"
RATE_ENV = "LA548_HIL_RATE"
CARD_ENV = "LA548_HIL_CARD"
PROFILE_ENV = "LA548_HIL_PROFILE"

DEFAULT_CHANNELS: tuple[str, ...] = ("FL", "FR")
DEFAULT_SAMPLE_RATE = 48000
MIN_CHANNELS = 2


class LoopbackConfigError(ValueError):
    """One or more LA548_HIL_* variables are missing or invalid."""


@dataclass(frozen=True)
class LoopbackConfig:
    sink: str
    source: str
    playback_channels: tuple[str, ...] = DEFAULT_CHANNELS
    capture_channels: tuple[str, ...] = DEFAULT_CHANNELS
    sample_rate: int = DEFAULT_SAMPLE_RATE
    card: str | None = None
    profile: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> LoopbackConfig:
        """Read the LA548_HIL_* variables, reporting every missing or invalid
        one in a single LoopbackConfigError."""
        problems: list[str] = []
        sink = _text(env, SINK_ENV)
        source = _text(env, SOURCE_ENV)
        if not sink:
            problems.append(f"{SINK_ENV} is not set (the PipeWire playback node name)")
        if not source:
            problems.append(f"{SOURCE_ENV} is not set (the PipeWire capture node name)")
        playback = _channels(env, PLAYBACK_CHANNELS_ENV, problems)
        capture = _channels(env, CAPTURE_CHANNELS_ENV, problems)
        sample_rate = _sample_rate(env, problems)
        card = _text(env, CARD_ENV) or None
        profile = _text(env, PROFILE_ENV) or None
        if card is not None and profile is None:
            problems.append(f"{PROFILE_ENV} must be set together with {CARD_ENV}")
        if profile is not None and card is None:
            problems.append(f"{CARD_ENV} must be set together with {PROFILE_ENV}")
        if problems:
            raise LoopbackConfigError(
                "loopback audio interface is not configured: " + "; ".join(problems)
                + " (see hardware_test/README.md)"
            )
        return cls(sink, source, playback, capture, sample_rate, card, profile)


def _text(env: Mapping[str, str], name: str) -> str:
    return env.get(name, "").strip()


def _channels(env: Mapping[str, str], name: str, problems: list[str]) -> tuple[str, ...]:
    raw = _text(env, name)
    if not raw:
        return DEFAULT_CHANNELS
    channels = tuple(part.strip() for part in raw.split(","))
    if any(not channel for channel in channels):
        problems.append(f"{name}={raw!r} has an empty channel name")
    elif len(channels) < MIN_CHANNELS:
        problems.append(f"{name}={raw!r} needs at least {MIN_CHANNELS} comma-separated channel names")
    elif len(set(channels)) != len(channels):
        problems.append(f"{name}={raw!r} repeats a channel name")
    return channels


def _sample_rate(env: Mapping[str, str], problems: list[str]) -> int:
    raw = _text(env, RATE_ENV)
    if not raw:
        return DEFAULT_SAMPLE_RATE
    try:
        rate = int(raw)
    except ValueError:
        rate = 0
    if rate <= 0:
        problems.append(f"{RATE_ENV}={raw!r} is not a positive integer sample rate")
    return rate


def _pactl(*args: str) -> str:
    # LC_ALL=C keeps the "Name:"/"Active Profile:" labels untranslated.
    result = subprocess.run(
        ["pactl", *args], capture_output=True, text=True, check=True, env={**os.environ, "LC_ALL": "C"}
    )
    return result.stdout


def active_profile(card: str) -> str:
    """The active profile of pactl card `card`."""
    in_card = False
    for line in _pactl("list", "cards").splitlines():
        stripped = line.strip()
        if stripped.startswith("Name:"):
            in_card = stripped == f"Name: {card}"
        elif in_card and stripped.startswith("Active Profile:"):
            return stripped.split(":", 1)[1].strip()
    raise LoopbackConfigError(f"{CARD_ENV}={card!r} is not listed by `pactl list cards`")


def ensure_profile(config: LoopbackConfig) -> None:
    """Switch the configured card to the configured profile if it isn't
    already active, and check that the switch took. No-op when unset."""
    if config.card is None or config.profile is None:
        return
    if active_profile(config.card) == config.profile:
        return
    subprocess.run(["pactl", "set-card-profile", config.card, config.profile], check=True)
    if active_profile(config.card) != config.profile:
        raise RuntimeError(f"failed to switch card {config.card!r} to profile {config.profile!r}")
