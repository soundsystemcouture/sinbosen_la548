"""Ties the LA548 Client to a loopback audio interface for HIL tests.

Wiring: any audio interface with at least two outputs and two inputs, with
playback and capture on one clock (normally the same device). The first two
configured playback channels drive LA548 inputs A/B, and LA548 outputs 1/2
feed the first two configured capture channels (see loopback.py and
hardware_test/README.md). Channel_id 0/1 = physical A/B on the input side and
physical output 1/2 on the output side, confirmed by test_00.

If an output reads silent while its routing, mute and processing all read
back correctly, power-cycle the LA548 before suspecting the jack.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import numpy as np

from sinbosen_la548.client import Client
from sinbosen_la548.transport import DEFAULT_PORT

from . import audio_io
from .loopback import DEFAULT_CHANNELS, DEFAULT_SAMPLE_RATE, LoopbackConfig, ensure_profile

DEFAULT_SETTLE_S = 0.3

INPUT_CHANNEL_IDS: tuple[int, int] = (0, 1)  # physical input A, B
OUTPUT_CHANNEL_IDS: tuple[int, int] = (0, 1)  # physical output 1, 2


class Rig:
    """A connected LA548 Client plus everything needed to drive/measure it over the loopback interface."""

    def __init__(
        self,
        client: Client,
        *,
        sink_name: str,
        source_name: str,
        playback_channels: tuple[str, ...] = DEFAULT_CHANNELS,
        capture_channels: tuple[str, ...] = DEFAULT_CHANNELS,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        settle_s: float = DEFAULT_SETTLE_S,
    ) -> None:
        self.client = client
        self.sink_name = sink_name
        self.source_name = source_name
        self.playback_channels = playback_channels
        self.capture_channels = capture_channels
        self.sample_rate = sample_rate
        self.settle_s = settle_s

    @classmethod
    def connect(
        cls,
        ip: str,
        *,
        port: int = DEFAULT_PORT,
        settle_s: float = DEFAULT_SETTLE_S,
        loopback: LoopbackConfig | None = None,
    ) -> Rig:
        """Connect to the LA548 at `ip`, using `loopback` or, when omitted,
        the LA548_HIL_* environment (LoopbackConfig.from_env)."""
        config = loopback if loopback is not None else LoopbackConfig.from_env()
        ensure_profile(config)
        client = Client(ip, port)
        client.connect()
        return cls(
            client,
            sink_name=config.sink,
            source_name=config.source,
            playback_channels=config.playback_channels,
            capture_channels=config.capture_channels,
            sample_rate=config.sample_rate,
            settle_s=settle_s,
        )

    def __enter__(self) -> Rig:
        return self

    def __exit__(self, *exc: object) -> None:
        self.client.close()

    def measure(self, signal: np.ndarray, **play_kwargs: Any) -> np.ndarray:
        """Play `signal` and capture the LA548's response, with no write in between.
        Configured playback channels beyond the signal's own columns play silence."""
        extra = len(self.playback_channels) - signal.shape[1] if signal.ndim == 2 else 0
        if extra > 0:
            signal = np.pad(signal, ((0, 0), (0, extra)))
        return audio_io.play_and_record(
            signal,
            sink_name=self.sink_name,
            source_name=self.source_name,
            playback_channels=self.playback_channels,
            capture_channels=self.capture_channels,
            sample_rate=self.sample_rate,
            **play_kwargs,
        )

    def write_then_measure(self, write_fn: Callable[[], Any], signal: np.ndarray, **play_kwargs: Any) -> np.ndarray:
        """Apply a Client write, let the DSP settle, then play+capture."""
        write_fn()
        time.sleep(self.settle_s)
        return self.measure(signal, **play_kwargs)

    def reset_channel_safe(self, channel_id: int, *, output: bool = False) -> None:
        """Bring one channel to a known, unambiguous baseline: unmuted, 0 dB, normal
        phase, zero delay. EQ and dynamics are left alone; the `rig` fixture's
        snapshot restore puts those back after every test."""
        self.client.set_mute(channel_id, False, output=output)
        self.client.set_volume(channel_id, 0.0, output=output)
        self.client.set_phase(channel_id, False, output=output)
        self.client.set_delay(channel_id, 0.0, output=output)

    def reset_both_channels_safe(self) -> None:
        """reset_channel_safe on both input and output channel_ids of the
        2-in/2-out loopback, and re-pin the mix-matrix routes it depends on
        (A->out0, B->out1) every time rather than trusting whatever routing
        the device was last left in - manual or app-side changes between runs
        have been observed to leave stray or missing routes."""
        for channel_id in INPUT_CHANNEL_IDS:
            self.reset_channel_safe(channel_id, output=False)
        for channel_id in OUTPUT_CHANNEL_IDS:
            self.reset_channel_safe(channel_id, output=True)
        for out_idx in range(8):
            self.client.set_mix_route(0, out_idx, out_idx == 0)
            self.client.set_mix_route(1, out_idx, out_idx == 1)
