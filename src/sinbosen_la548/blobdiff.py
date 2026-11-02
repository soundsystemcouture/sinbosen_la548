"""Byte-level diffing for settings blobs (`Client.read_group()`).

Only the DA0408X layout of the settings blob is decoded (`settings.py`,
docs/SETTINGS_READBACK.md). For anything else, this is the cheap
alternative: dump the group before a known change (e.g. `Client.set_mute`),
make the change, dump again, and diff the two blobs to empirically locate
which byte offset a given control lives at. Findings from doing this belong
in docs/PROTOCOL.md, "Write path".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ByteDiff:
    offset: int
    before: int
    after: int


def diff_blobs(before: bytes, after: bytes) -> list[ByteDiff]:
    """Return every byte offset where `before` and `after` differ.

    Compares only up to the shorter length. A length mismatch usually means
    the two dumps aren't the same group/model - the caller should treat the
    whole comparison with suspicion in that case, not just the missing tail.
    """
    return [ByteDiff(i, b, a) for i, (b, a) in enumerate(zip(before, after, strict=False)) if b != a]
