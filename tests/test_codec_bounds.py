"""Encoder bounds (codecs.py, docs/PROTOCOL.md "Value bounds") and the wire
byte offset of every struct field."""

import math

import pytest

from sinbosen_la548 import codecs

# Wire layout of each struct payload: packed size in bytes, then each field's
# inclusive (first, last) byte range.
_DYNAMICS = (10, {"threshold": (0, 1), "start_time": (2, 3), "release_time": (4, 5), "ratio": (6, 7),
                  "enabled": (8, 8), "detection_type": (9, 9)})
LAYOUTS: dict[str, tuple[int, dict[str, tuple[int, int]]]] = {
    "eq": (9, {"freq": (0, 1), "gain": (2, 3), "q": (4, 5), "slope": (6, 6), "type": (7, 7), "enabled": (8, 8)}),
    "xover": (5, {"freq": (0, 1), "filter_type": (2, 2), "slope": (3, 3), "enabled": (4, 4)}),
    "dyneq": (18, {"freq": (0, 1), "gain": (2, 3), "q": (4, 5), "slope": (6, 6), "type": (7, 7), "mode": (8, 8),
                   "threshold": (9, 10), "start_time": (11, 12), "release_time": (13, 14), "ratio": (15, 16),
                   "enabled": (17, 17)}),
    "compressor": _DYNAMICS,
    "limiter": _DYNAMICS,
    "expander": _DYNAMICS,
    "input_source": (6, {"source": (0, 0), "freq": (1, 2), "gain": (3, 4), "enabled": (5, 5)}),
    "freq_shifter": (3, {"level": (0, 0), "direction": (1, 1), "enabled": (2, 2)}),
    "fir": (9, {"enabled": (0, 0), "filter_type": (1, 1), "window_type": (2, 2), "freq_low": (3, 4),
                "freq_high": (5, 6), "attenuation": (7, 7), "order": (8, 8)}),
    "mix_route": (3, {"input": (0, 0), "output": (1, 1), "enabled": (2, 2)}),
}


SCALAR_BOUNDS = [
    (codecs.volume_u16, -80.0, 12.0, 0.1),
    (codecs.dyneq_threshold_u16, -60.0, 20.0, 0.5),
    (codecs.expander_threshold_u16, -100.0, 0.0, 0.5),
    (codecs.dyneq_starttime_u16, 0.3, 200.0, 0.1),
    (codecs.dyneq_release_time_u16, 50.0, 5000.0, 1.0),
    (codecs.dyneq_ratio_u16, 1.0, 20.0, 0.01),
    (codecs.compressor_ratio_u16, 1.0, 128.0, 0.1),
]


@pytest.mark.parametrize(("codec", "low", "high", "step"), SCALAR_BOUNDS)
def test_scalar_codecs_accept_supported_limits_and_reject_beyond(codec, low, high, step):
    assert codec.decode(codec.encode(low)) == pytest.approx(low)
    assert codec.decode(codec.encode(high)) == pytest.approx(high)
    for bad in (low - step, high + step, math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError, match="out of range"):
            codec.encode(bad)


def test_volume_floor_is_not_defeated_by_truncation():
    # int((-80.09 + 80) * 10) == 0 would otherwise encode as -80 dB.
    with pytest.raises(ValueError, match="out of range"):
        codecs.volume_u16.encode(-80.09)


def test_volume_ceiling_is_raw_920():
    assert codecs.volume_u16.encode(12.0) == (920).to_bytes(2, "little")
    with pytest.raises(ValueError, match="out of range"):
        codecs.volume_u16.encode(12.01)


def test_input_source_gain_and_frequency_bounds():
    encode = codecs.input_source_struct.encode
    assert encode((1, 20000, 12.0, True))[1:3] == (20000).to_bytes(2, "little")
    assert encode((0, 0, -80.0, True))  # factory readback stores freq 0
    for fields in ((1, 20001, 0.0, True), (1, 1000, 12.1, True), (1, 1000, -80.1, True)):
        with pytest.raises(ValueError, match="out of range"):
            encode(fields)


@pytest.mark.parametrize(
    "fields",
    [
        (True, 0, 0, 9, 1000, 40.0, 0),
        (True, 0, 0, 100, 20001, 40.0, 0),
        (True, 0, 0, 100, 1000, 20.9, 0),
        (True, 0, 0, 100, 1000, 120.5, 0),
        (True, 0, 12, 100, 1000, 40.0, 0),
    ],
)
def test_fir_struct_rejects_values_outside_supported_ranges(fields):
    with pytest.raises(ValueError, match="out of range"):
        codecs.fir_struct.encode(fields)


def test_fir_struct_accepts_supported_limits():
    payload = codecs.fir_struct.encode((True, 0, 11, 10, 20000, 120.0, 1))
    assert codecs.fir_struct.decode(payload) == (True, 0, 11, 10, 20000, 120.0, 1)


@pytest.mark.parametrize("slope", [2, 7, -1, True])
def test_eq_struct_rejects_slopes_outside_the_supported_set(slope):
    with pytest.raises(ValueError, match="slope out of range"):
        codecs.eq_struct.encode((1000, 0.0, 1.0, slope, 1, True))


@pytest.mark.parametrize("layout", LAYOUTS)
def test_wire_layouts_tile_their_packed_size(layout):
    size, fields = LAYOUTS[layout]
    covered = sorted(i for first, last in fields.values() for i in range(first, last + 1))
    assert covered == list(range(size))


# (codec, layout, layout field per tuple position, base, alternate)
STRUCT_LAYOUTS = [
    pytest.param(codecs.eq_struct, "eq", ["freq", "gain", "q", "slope", "type", "enabled"],
                 (1000, 0.0, 1.0, 0, 0, False), (2000, 6.0, 2.0, 1, 1, True), id="eq"),
    pytest.param(codecs.xover_struct, "xover", ["freq", "filter_type", "slope", "enabled"],
                 (1000, 0, 12, False), (2000, 1, 24, True), id="xover"),
    pytest.param(codecs.dyneq_struct, "dyneq",
                 ["freq", "gain", "q", "slope", "type", "mode", "threshold", "start_time", "release_time", "ratio",
                  "enabled"],
                 (1000, 0.0, 1.0, 0, 0, 0, -20.0, 1.0, 100.0, 2.0, False),
                 (2000, 6.0, 2.0, 1, 1, 1, -10.0, 5.0, 200.0, 4.0, True), id="dyneq"),
    pytest.param(codecs.compressor_struct, "compressor",
                 ["threshold", "start_time", "release_time", "ratio", "enabled"],
                 (-20.0, 1.0, 100.0, 2.0, False), (-10.0, 5.0, 200.0, 4.0, True), id="compressor"),
    pytest.param(codecs.limiter_struct, "limiter",
                 ["threshold", "start_time", "release_time", "enabled", "detection_type"],
                 (-20.0, 1.0, 100.0, False, 0), (-10.0, 5.0, 200.0, True, 1), id="limiter"),
    pytest.param(codecs.expander_struct, "expander",
                 ["threshold", "start_time", "release_time", "ratio", "enabled"],
                 (-50.0, 1.0, 100.0, 2.0, False), (-40.0, 5.0, 200.0, 4.0, True), id="expander"),
    pytest.param(codecs.input_source_struct, "input_source", ["source", "freq", "gain", "enabled"],
                 (0, 1000, -10.0, False), (1, 2000, -5.0, True), id="input_source"),
    pytest.param(codecs.freq_shifter_struct, "freq_shifter", ["level", "direction", "enabled"],
                 (0, 0, False), (3, 1, True), id="freq_shifter"),
    pytest.param(codecs.fir_struct, "fir",
                 ["enabled", "filter_type", "window_type", "freq_low", "freq_high", "attenuation", "order"],
                 (False, 0, 0, 100, 1000, 40.0, 0), (True, 1, 1, 200, 2000, 60.0, 1), id="fir"),
    pytest.param(codecs.mix_route_u8, "mix_route", ["input", "output", "enabled"],
                 (0, 0, False), (1, 2, True), id="mix_route"),
]


@pytest.mark.parametrize(("codec", "layout", "names", "base", "alternate"), STRUCT_LAYOUTS)
def test_each_struct_field_lands_at_its_wire_offset(codec, layout, names, base, alternate):
    size, fields = LAYOUTS[layout]
    reference = codec.encode(base)
    assert len(reference) == size
    for position, name in enumerate(names):
        first, last = fields[name]
        changed = list(base)
        changed[position] = alternate[position]
        payload = codec.encode(tuple(changed))
        moved = {i for i, (a, b) in enumerate(zip(reference, payload, strict=True)) if a != b}
        assert moved, f"{layout}.{name} did not change the payload"
        expected = set(range(first, last + 1))
        assert moved <= expected, f"{layout}.{name} wrote bytes {sorted(moved)}, expected {first}-{last}"
