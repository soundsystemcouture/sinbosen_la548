# Settings readback

`settings.decode_settings(blob, *, profile=...)` decodes a complete
`Client.read_group()` reply into immutable typed settings; the default profile
is the `DA0408X` layout. `Client.read_settings(*, profile=..., timeout=2.0)`
performs exactly one ReadGroup request and delegates to that pure decoder.
Neither API writes, connects automatically, or reads FIR coefficients.

```python
from pathlib import Path
from sinbosen_la548.settings import decode_settings

settings = decode_settings(Path("tests/fixtures/read_group_sample.bin").read_bytes())
print(settings.outputs[0].hp)
print(settings.inputs[0].eq_bands[0])
print(settings.mix_matrix[0][0])  # input A -> output 1
```

For live readback, use `with Client(ip) as client: settings = client.read_settings()`.
Existing CLI commands and raw backup APIs retain their behavior.

## Profile and evidence

The selected default is **the DA0408X layout**, not a claim that firmware
identity has been discovered. The fixture and live-tested channel counts and
mixer layout match this profile. No confirmed opcode reports the device's
model variant or sample rate.

| Profile property | Value | Evidence |
| --- | --- | --- |
| Inputs / outputs | 4 / 8 | `read_mix_matrix` bus count and `channel_copy` widths on hardware; fixture |
| Input / output EQ bands | 7 / 15 | Fixture band frequencies and block boundaries within each channel stride |
| Input dynamic EQ bands | 3 | Fixture block boundaries; dynamic EQ writes confirmed by readback diff |
| Model sample rate | 96000 Hz, assumed | Not read from device; the filter models in [FILTER_MODELS.md](FILTER_MODELS.md) fit measured responses at this rate |
| Input compressor | Unavailable | No input compressor block in this layout; an input compressor write leaves the readback unchanged on hardware |
| Snapshot size | 2464 bytes | 2164 packed settings bytes plus 12 names of 25 bytes |

The decoder accepts only this profile. Exact length, mixer coordinates, known
boolean encodings, EQ frequency/gain/Q bounds, crossover frequency/slope bounds,
and delay bounds are checked. This establishes compatibility with the supported
shape; it is not a firmware detector or permission to assume every label in
the manufacturer's app matches measured behavior.

### Basis

Block positions were located on hardware by diffing `read_group()` dumps taken
before and after single-control writes (`src/sinbosen_la548/blobdiff.py`,
`sinbosen-la548 diff-group`). The output crossover stores HP before LP; the
fixture holds 20 Hz then 20000 Hz in those positions. Snapshot mixer entries
are ordered **(output, input, value)**, the reverse of the write payload's
(input, output, value); the decoder uses the live-confirmed read order.
`tests/test_settings.py` checks block boundaries, strides, HP/LP order, and
every mixer entry against the existing `_parse_mix_matrix()` parser, using
`tests/fixtures/read_group_sample.bin` (SHA-256
`c7f0a61275384bc27ffa5b823584ebad732a1a555a972082014e4ef33ff5933b`).

## Packed offsets

All multi-byte integer fields are little-endian. Input channel `i` starts at
`153*i`; output channel `o` starts at `612 + 182*o`. Offsets below are relative
to each channel's start.

| Input block | Offset | Bytes |
| --- | ---: | ---: |
| Base | 0 | 8 |
| Input source | 8 | 6 |
| Expander | 14 | 10 |
| Dynamic EQ, 3 × 18 | 24 | 54 |
| EQ, 7 × 9 | 78 | 63 |
| Frequency shifter | 141 | 3 |
| FIR parameter snapshot | 144 | 9 |

| Output block | Offset | Bytes |
| --- | ---: | ---: |
| Base | 0 | 8 |
| Compressor | 8 | 10 |
| Limiter | 18 | 10 |
| HP crossover | 28 | 5 |
| LP crossover | 33 | 5 |
| EQ, 15 × 9 | 38 | 135 |
| FIR parameter snapshot | 173 | 9 |

The mixer occupies absolute bytes 2068–2163, input-major, 32 × 3 bytes.
Names occupy bytes 2164–2463, inputs first then outputs, 25 bytes each. Names
are decoded as UTF-8 with replacement because their actual encoding is unknown;
the full original slot remains available as `name_raw`, including bytes after NUL.

## Data contracts and preservation

- `DeviceSettings`: `profile`, `inputs`, `outputs`, `mix_matrix`, and the full `raw`
  snapshot. Matrix indexing is `[input_id][output_id]`.
- `ChannelSettings`: zero-based `channel_id`, `output`, `name`, `name_raw`, `base`,
  `eq_bands`, `fir`, channel `raw`, and optional direction-specific blocks.
  `None`/empty means unavailable in this layout, not an invented live value.
- Base exposes `muted`, `inverted`, `gain_db`, `delay_ms`, and unresolved
  `source_raw`/`fbx_raw`. Mute uses the existing inverted codec; polarity uses
  the existing identity codec.
- EQ, crossovers, dynamic EQ, compressor, limiter, expander, input source, and
  frequency shifter decode through the existing codecs and preserve block `raw`.
- EQ types 3 (AllPass1, first-order) and 5 (Notch, negative gain only) are
  confirmed by hardware-in-the-loop measurement and named accordingly. Type 5
  switches bandwidth conventions at -6 dB; shallow cuts use peaking-EQ coefficients.
  The formula, branch, and measurement coverage are documented in
  [FILTER_MODELS.md](FILTER_MODELS.md).
  Readback does not silently zero an allpass band's stored gain or slope.
- `FIRSnapshot.from_bytes()` validates the 9-byte boundary and enable flag only.
  Its `enabled` and `raw` fields are independent of the evolving FIR codecs and
  coefficient interpretation.
- All models are frozen dataclasses; collections are tuples; raw buffers are
  owned immutable bytes, even when the caller supplies a mutable buffer.

Readback validation deliberately differs from validation for a new write.
The fixture contains disabled output compressors with threshold +340 dB and
ratio 0. These values are decoded faithfully. Unused detectionType/ratio bytes,
unresolved base fields, enum values, and FIR bytes remain intact. Apply
validates only edited blocks and does not rewrite unrelated disabled blocks
from assumed defaults. No snapshot re-encoder is provided.

## Verification limits

The decoder's unit tests use a recorded hardware snapshot, synthetic changes to
that snapshot, and fake transport tests; they perform no live network
operations and no hardware writes. Unit tests establish field boundaries and
codec semantics; they do not establish the audible behavior of every field.

Still open for opt-in hardware acceptance: compare decoded controls with the
manufacturer's app for the active unit and confirm readback after deliberate
changes. The sample rate is an assumption until it can be read from the device.
