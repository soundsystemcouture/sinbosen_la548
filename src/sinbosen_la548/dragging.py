"""Session-scoped graph edits. Previews never stage or write device settings."""

from __future__ import annotations

import math
import threading
import uuid
from dataclasses import dataclass, replace

from .service import ControlKey, DSPService, ServiceView, normalize_control
from .settings import ChannelSettings, EQBandSettings
from .tuning import EQ_TYPES


def drag_frequency(value: float) -> int:
    if not math.isfinite(value):
        raise ValueError("frequency must be finite")
    return min(20000, max(10, math.floor(value + 0.5)))


def frequency_at_position(position: float, low: float, high: float) -> int:
    if not all(math.isfinite(v) for v in (position, low, high)) or not 0 < low <= high:
        raise ValueError("invalid frequency axis")
    return drag_frequency(10 ** (math.log10(low) + min(1, max(0, position)) * math.log10(high / low)))


def pending_label(view: ServiceView, *, unfinished: bool = False) -> str:
    count = len(view.pending)
    channels = len({(change.key.output, change.key.channel) for change in view.pending})
    label = (f"**Unapplied changes: {count} controls across {channels} channels.**" if count
             else "**No unapplied changes.**")
    if view.demo:
        label += " · Offline demo — no device writes."
    if unfinished:
        label += " · **Unfinished input — correct highlighted values before Apply.**"
    return label


@dataclass(frozen=True)
class DragContext:
    id: str
    session: str
    choice: str
    band: int
    revision: int
    connected: bool
    demo: bool
    options: tuple[bool, bool, bool, bool]
    selected: int  # the selected band when the drag began (may differ from `band`)


class DragEdits:
    def __init__(self, service: DSPService) -> None:
        self.service = service
        self._active: dict[str, DragContext] = {}
        self._lock = threading.RLock()

    @staticmethod
    def channel(view: ServiceView, choice: str) -> ChannelSettings:
        direction, index = choice.split(":")
        if direction not in ("in", "out"):
            raise ValueError("unknown channel direction")
        channels = view.draft.outputs if direction == "out" else view.draft.inputs
        if not 0 <= int(index) < len(channels):
            raise ValueError("channel out of range")
        return channels[int(index)]

    def begin(self, session: str, choice: str, band: int,
              options: tuple[bool, bool, bool, bool], *, selected: int | None = None) -> DragContext:
        with self._lock:
            view = self.service.view(session)
            channel = self.channel(view, choice)
            if not 0 <= band < len(channel.eq_bands) or channel.eq_bands[band].type not in EQ_TYPES:
                raise ValueError("this band cannot be dragged")
            self.service.set_live(session, False)
            context = DragContext(uuid.uuid4().hex, session, choice, band, view.revision,
                                  view.connected, view.demo, options, band if selected is None else selected)
            self._active[session] = context
            return context

    def current(self, session: str, choice: str) -> DragContext | None:
        """The session's active drag, unless the draft revision, connection,
        demo/live mode or selected channel changed since it began. The band
        is not compared: it lives in the context, and the selected-band value
        a UI event carries can lag the drag that just selected it."""
        with self._lock:
            context = self._active.get(session)
            if context is None:
                return None
            view = self.service.view(session)
            if (context.choice, context.revision, context.connected, context.demo) != (
                    choice, view.revision, view.connected, view.demo) or view.live:
                self._active.pop(session, None)
                return None
            return context

    def preview(self, session: str, identifier: str, frequency: float) -> tuple[DragContext, ServiceView,
                                                                              ChannelSettings]:
        with self._lock:
            context = self._active.get(session)
            if context is None or context.id != identifier or self.current(session, context.choice) is None:
                raise ValueError("drag expired; select the band and try again")
            view = self.service.view(session)
            channel = self.channel(view, context.choice)
            bands = list(channel.eq_bands)
            key = ControlKey(channel.output, channel.channel_id, "eq", context.band)
            normalized = normalize_control(key, replace(bands[context.band], freq_hz=drag_frequency(frequency)))
            assert isinstance(normalized, EQBandSettings)
            bands[context.band] = normalized
            return context, view, replace(channel, eq_bands=tuple(bands))

    def finish(self, session: str, identifier: str, frequency: float) -> None:
        with self._lock:
            context, _, channel = self.preview(session, identifier, frequency)
            self.service.stage(session, ControlKey(channel.output, channel.channel_id, "eq", context.band),
                               channel.eq_bands[context.band], expected_revision=context.revision)
            self._active.pop(session, None)

    def is_new_selection(self, context: DragContext, index: int) -> bool:
        """Whether a band-selection event picked a band other than the one
        being dragged. An event still carrying the selection from before the
        drag began (Gradio can deliver one late, e.g. on dropdown blur) is
        not a new choice."""
        return index not in (context.band, context.selected)

    def cancel(self, session: str) -> None:
        with self._lock:
            self._active.pop(session, None)
