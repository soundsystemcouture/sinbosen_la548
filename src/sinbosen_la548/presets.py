"""Versioned local presets stage supported controls; they are not raw snapshots."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, fields, is_dataclass, replace
from typing import Any

from .service import ControlKey, _semantic, controls
from .settings import DeviceSettings, FIRSnapshot


def _value(value: object) -> object:
    if isinstance(value, FIRSnapshot):
        return {"raw_hex": value.raw.hex()}
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: getattr(value, f.name) for f in fields(value) if f.name != "raw"}
    return value


def export_preset(settings: DeviceSettings) -> str:
    return json.dumps({"format": "sinbosen-la548-preset", "version": 1,
                       "profile": asdict(settings.profile),
                       "controls": [{"key": asdict(key), "value": _value(value)}
                                    for key, value in controls(settings).items()]}, indent=2, allow_nan=False)


def import_preset(settings: DeviceSettings, text: str) -> dict[ControlKey, object]:
    if len(text) > 1024 * 1024:
        raise ValueError("preset exceeds 1 MiB")
    document = json.loads(text)
    if not isinstance(document, dict) or document.get("format") != "sinbosen-la548-preset":
        raise ValueError("not an LA548 local preset")
    if document.get("version") != 1 or document.get("profile") != asdict(settings.profile):
        raise ValueError("preset version/profile is incompatible")
    entries = document.get("controls")
    if not isinstance(entries, list):
        raise ValueError("preset has no control list")
    current = controls(settings)
    imported: dict[ControlKey, object] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"key", "value"}:
            raise ValueError("malformed preset control")
        key_data = entry["key"]
        if (not isinstance(key_data, dict) or set(key_data) != {"output", "channel", "block", "index"}
                or not isinstance(key_data["output"], bool) or type(key_data["channel"]) is not int
                or type(key_data["index"]) is not int or not isinstance(key_data["block"], str)):
            raise ValueError("malformed preset control key")
        key = ControlKey(**key_data)
        if key not in current or key in imported:
            raise ValueError("unsupported or duplicate preset control")
        old, value = current[key], entry["value"]
        if isinstance(old, FIRSnapshot):
            if not isinstance(value, dict) or set(value) != {"raw_hex"} or not isinstance(value["raw_hex"], str):
                raise ValueError("malformed FIR adapter block")
            value = FIRSnapshot.from_bytes(bytes.fromhex(value["raw_hex"]))
        elif is_dataclass(old) and not isinstance(old, type):
            expected = {f.name for f in fields(old) if f.name != "raw"}
            if not isinstance(value, dict) or set(value) != expected:
                raise ValueError("malformed preset block fields")
            for name, supplied in value.items():
                existing = getattr(old, name)
                if isinstance(existing, bool) and not isinstance(supplied, bool):
                    raise ValueError(f"{name} requires a boolean")
                if isinstance(existing, int) and not isinstance(existing, bool) and type(supplied) is not int:
                    raise ValueError(f"{name} requires an integer")
                if isinstance(existing, float) and (not isinstance(supplied, (int, float))
                                                    or isinstance(supplied, bool) or not math.isfinite(supplied)):
                    raise ValueError(f"{name} requires a finite number")
            updates: dict[str, Any] = value
            value = replace(old, **updates)
        elif isinstance(old, bool):
            if not isinstance(value, bool):
                raise ValueError("preset boolean is invalid")
        elif isinstance(old, (int, float)) and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            raise ValueError("preset number is invalid")
        imported[key] = value
    if imported.keys() != current.keys():
        raise ValueError("preset control structure is incomplete")
    return {key: value for key, value in imported.items() if _semantic(current[key]) != _semantic(value)}
