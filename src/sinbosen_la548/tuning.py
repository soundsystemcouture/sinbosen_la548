"""Core tuning actions shared by UI callbacks and offline tests."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .service import ControlKey, DSPService
from .settings import ChannelSettings

EQ_TYPES = {0: "Parametric EQ", 1: "Low shelf", 2: "High shelf", 3: "Allpass (1st order)",
           4: "Allpass (2nd order)", 5: "Notch"}
XOVER_TYPES = {0: "Butterworth", 1: "Linkwitz–Riley", 2: "Bessel"}
# Both allpass types ignore gain on hardware (response.py), so the UI pins it to 0 dB.
ALLPASS_TYPES = frozenset({3, 4})


class TuningController:
    def __init__(self, service: DSPService):
        self.service = service
        self._bypass_masks: dict[tuple[str, bool, int], tuple[bool, ...]] = {}

    def reset(self, token: str) -> None:
        for key in list(self._bypass_masks):
            if key[0] == token:
                self._bypass_masks.pop(key)

    def channel(self, token: str, output: bool, channel: int) -> ChannelSettings:
        settings = self.service.view(token).draft
        channels = settings.outputs if output else settings.inputs
        if not 0 <= channel < len(channels):
            raise ValueError("channel out of range")
        return channels[channel]

    def eq(self, token: str, output: bool, channel: int, index: int, field: str, value: Any,
           *, immediate: bool = False) -> None:
        ch = self.channel(token, output, channel)
        if not 0 <= index < len(ch.eq_bands):
            raise ValueError("EQ band out of range")
        if field not in {"freq_hz", "gain_db", "q", "type", "enabled"}:
            raise ValueError("unsupported EQ field")
        band = ch.eq_bands[index]
        if field in {"freq_hz", "type"}:
            value = int(value)
        updates: dict[str, Any] = {field: value}
        if field == "type" and value != band.type:
            if value not in EQ_TYPES:
                raise ValueError("unresolved EQ types cannot be selected as new filters")
            updates["slope"] = 0
            if value in ALLPASS_TYPES:
                updates["gain_db"] = 0.0
        if field == "gain_db" and band.type in ALLPASS_TYPES:
            raise ValueError("allpass gain is fixed; adjust frequency and Q")
        self.service.stage(token, ControlKey(output, channel, "eq", index), replace(band, **updates),
                           immediate=immediate)

    def crossover(self, token: str, channel: int, band: str, field: str, value: Any,
                  *, immediate: bool = False) -> None:
        if band not in {"hp", "lp"}:
            raise ValueError("unknown crossover band")
        ch = self.channel(token, True, channel)
        block = ch.hp if band == "hp" else ch.lp
        if block is None:
            raise ValueError("crossover unavailable")
        if field not in {"freq_hz", "filter_type", "slope_db_per_oct", "enabled"}:
            raise ValueError("unsupported crossover field")
        if field != "enabled":
            value = int(value)
        updates: dict[str, Any] = {field: value}
        self.service.stage(token, ControlKey(True, channel, band), replace(block, **updates), immediate=immediate)

    def add_allpass(self, token: str, output: bool, channel: int) -> int:
        ch = self.channel(token, output, channel)
        if (token, output, channel) in self._bypass_masks:
            raise ValueError("restore EQ before allocating an allpass slot")
        for index, band in enumerate(ch.eq_bands):
            if not band.enabled:
                self.service.stage(token, ControlKey(output, channel, "eq", index),
                                   replace(band, freq_hz=80, gain_db=0.0, q=1.0, slope=0, type=4, enabled=True))
                return index
        raise ValueError("no disabled EQ slot is available for an allpass")

    def bypass(self, token: str, output: bool, channel: int, *, restore: bool = False) -> None:
        ch = self.channel(token, output, channel)
        key = (token, output, channel)
        mask = self._bypass_masks.get(key)
        if restore and mask is None:
            raise ValueError("there is no saved EQ bypass state to restore")
        if not restore and mask is None:
            mask = tuple(b.enabled for b in ch.eq_bands)
        self.service.stage_many(token, {
            ControlKey(output, channel, "eq", i): replace(band, enabled=mask[i] if restore and mask else False)
            for i, band in enumerate(ch.eq_bands)
        })
        if restore:
            self._bypass_masks.pop(key, None)
        elif mask is not None:
            self._bypass_masks[key] = mask
