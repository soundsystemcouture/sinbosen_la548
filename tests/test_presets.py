import json
from dataclasses import replace
from pathlib import Path

import pytest

from sinbosen_la548.fir_adapter import FIRParameters
from sinbosen_la548.presets import export_preset, import_preset
from sinbosen_la548.service import ControlKey, DSPService, replace_control
from sinbosen_la548.settings import decode_settings


@pytest.fixture
def settings():
    return decode_settings((Path(__file__).parent / "fixtures/read_group_sample.bin").read_bytes())


def test_roundtrip_preserves_disabled_out_of_range_readback(settings):
    assert import_preset(settings, export_preset(settings)) == {}


def test_import_returns_only_changes_and_stages_without_writes(settings):
    key = ControlKey(True, 0, "delay")
    altered = replace_control(settings, key, 5.123)
    changes = import_preset(settings, export_preset(altered))
    assert changes == {key: 5.123}
    service = DSPService(settings)
    token = service.new_session()
    service.stage_many(token, changes)
    assert service.view(token).draft.outputs[0].base.delay_ms == 5.12
    assert service.view(token).baseline == settings


@pytest.mark.parametrize("mutation", ["profile", "version", "missing", "duplicate", "boolean", "field"])
def test_incompatible_presets_are_rejected(settings, mutation):
    data = json.loads(export_preset(settings))
    if mutation == "profile":
        data["profile"]["output_count"] = 4
    elif mutation == "version":
        data["version"] = 2
    elif mutation == "missing":
        data["controls"].pop()
    elif mutation == "duplicate":
        data["controls"].append(data["controls"][0])
    elif mutation == "boolean":
        data["controls"][0]["value"] = 0
    else:
        data["controls"][4]["value"]["enabled"] = 1
    with pytest.raises(ValueError):
        import_preset(settings, json.dumps(data))


@pytest.mark.parametrize("raw_hex", [None, 7, ["00"], "zz"])
def test_malformed_fir_raw_hex_raises_value_error(settings, raw_hex):
    data = json.loads(export_preset(settings))
    fir = next(c for c in data["controls"] if c["key"]["block"] == "fir")
    fir["value"]["raw_hex"] = raw_hex
    with pytest.raises(ValueError):
        import_preset(settings, json.dumps(data))


def test_fir_adapter_current_codec_and_da0408x_order_capability(settings):
    params = FIRParameters.read(settings.outputs[0].fir)
    assert FIRParameters.read(params.snapshot(output=True)) == params
    for output in (False, True):
        with pytest.raises(ValueError, match="0 or 1"):
            replace(params, order_index=2).snapshot(output=output)
