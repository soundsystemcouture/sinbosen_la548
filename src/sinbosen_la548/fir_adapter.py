"""DA0408X FIR parameter adapter."""

from __future__ import annotations

from dataclasses import dataclass

from . import codecs
from .settings import FIRSnapshot


@dataclass(frozen=True)
class FIRParameters:
    enabled: bool
    fir_type: int
    win_type: int
    freq_low_hz: int
    freq_high_hz: int
    attenuation_db: float
    order_index: int

    @classmethod
    def read(cls, snapshot: FIRSnapshot) -> FIRParameters:
        return cls(*codecs.fir_struct.decode(snapshot.raw))

    def snapshot(self, *, output: bool) -> FIRSnapshot:
        del output  # Retained for compatibility with direction-aware callers.
        if self.order_index not in (0, 1):
            raise ValueError("DA0408X FIR order index must be 0 or 1 (256 or 512 taps)")
        return FIRSnapshot.from_bytes(codecs.fir_struct.encode((
            self.enabled, self.fir_type, self.win_type, self.freq_low_hz, self.freq_high_hz,
            self.attenuation_db, self.order_index)))
