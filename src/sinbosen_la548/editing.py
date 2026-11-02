"""Local numeric edits, kept separate from validated DSP drafts."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class NumericKey:
    selection: str
    field: str
    band: int = -1


@dataclass
class NumericEdit:
    value: float | None
    due_at: float | None
    error: str = ""


class NumericEdits:
    def __init__(self) -> None:
        self._sessions: dict[str, dict[NumericKey, NumericEdit]] = {}

    def queue(self, session: str, key: NumericKey, value: float | None, *, now: float | None = None) -> None:
        due = (time.monotonic() if now is None else now) + 0.25
        self._sessions.setdefault(session, {})[key] = NumericEdit(value, due)

    def get(self, session: str, key: NumericKey) -> NumericEdit | None:
        return self._sessions.get(session, {}).get(key)

    def ready(self, session: str, *, now: float | None = None,
              force: bool = False, key: NumericKey | None = None) -> list[tuple[NumericKey, NumericEdit]]:
        current = time.monotonic() if now is None else now
        return [(k, edit) for k, edit in self._sessions.get(session, {}).items()
                if (key is None or k == key) and edit.due_at is not None and (force or edit.due_at <= current)]

    def finish(self, session: str, key: NumericKey, error: str = "") -> None:
        if error:
            edit = self._sessions[session][key]
            edit.error, edit.due_at = error, None
        else:
            self._sessions[session].pop(key, None)

    def has_errors(self, session: str) -> bool:
        return any(edit.error for edit in self._sessions.get(session, {}).values())

    def has_unfinished(self, session: str) -> bool:
        return bool(self._sessions.get(session))

    def discard(self, session: str, key: NumericKey) -> None:
        self._sessions.get(session, {}).pop(key, None)

    def drop(self, session: str) -> None:
        self._sessions.pop(session, None)


def validate_number(value: float | None, *, minimum: float, maximum: float,
                    label: str, unit: str = "", integer: bool = False) -> float:
    if value is None:
        raise ValueError(f"Enter {label.lower()}.")
    if not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number.")
    suffix = f" {unit}" if unit else ""
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum:g} and {maximum:g}{suffix}.")
    if integer and float(value) != int(value):
        units = f" ({unit})" if unit else ""
        raise ValueError(f"{label} must be a whole number{units}.")
    return value
