# sinbosen-la548

An independent Linux client, command-line tool and local web UI for the
Sinbosen LA548 network audio DSP, so the unit can be controlled without the
manufacturer's Windows-only app.

See [the protocol reference](docs/PROTOCOL.md) for the wire format and
control catalog, including what's confirmed live vs. still unverified.

## Status

Session handling and three read-only calls (`read_lock_state`, `get_user_name`,
`read_group`) are confirmed against real hardware. An earlier version of this
client sent a guessed "handshake" frame on connect. It was really opcode 0xA4,
the generic parameter write, and its default header fields address channel 1's
mute, so every connect silently muted channel 1. The client never sends that
frame; see "Unsafe opcodes" in docs/PROTOCOL.md.

The write path (`registry.py` + `codecs.py` + `Client._write_param`) is the
structural fix for that incident: every control is a `ParamSpec`, never
hand-typed header fields. Mute/Volume/Phase/Delay/Crossover/EQ
(including AllPass, for subwoofer time/phase alignment) - input and output -
plus input-only Dynamic EQ, Compressor/Limiter/Expander (Compressor is input
and output, Limiter is output-only, Expander is input-only), the mix matrix
(on/off per crosspoint - no continuous level path exists), Input Source/FIR
(input and output)/Frequency Shifter (input-only), and the whole-device
operations (program save/load, FIR coefficient backup/restore, channel copy -
**not** the raw whole-group write path, which stays deliberately unimplemented,
see docs/PROTOCOL.md) are implemented, unit-tested, and **confirmed on real
hardware** with the hardware-in-the-loop suite (`hardware_test/`, see its own
README) - every control the client implements
has been driven and measured on a real unit at least once. A few findings
from that process are real, documented hardware limitations rather than bugs
in this client - `eq_all_bypass`/`eq_all_reset` are confirmed non-functional
on this unit's firmware, and the input compressor has no effect on this
firmware - see docs/PROTOCOL.md, "Known device behaviour and open
questions" for the full list of what's confirmed vs. still genuinely open
(e.g. EQ types 3/5's true identity).

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
```

## Usage

The device's IP is shown on its own front panel - that's the reliable way to
find it (network discovery has not been made to work against this hardware;
see docs/PROTOCOL.md).

```sh
uv run sinbosen-la548 status <ip>
uv run sinbosen-la548 search                          # best-effort; may find nothing, see docs/PROTOCOL.md
uv run sinbosen-la548 dump-group <ip> backup.bin       # back up the active channel group to a file
uv run sinbosen-la548 set-mute <ip> 0 mute             # mute input channel 0 (--output for an output channel)
uv run sinbosen-la548 set-phase <ip> 0 invert
uv run sinbosen-la548 set-volume <ip> 0 -6.0
uv run sinbosen-la548 set-delay <ip> 0 12.5            # 12.5 ms
uv run sinbosen-la548 set-delay <ip> 0 12.5 --verify   # ...then read settings back and confirm the stored value
uv run sinbosen-la548 set-xover <ip> 0 hp --freq 80 --type 1 --slope 24 --enable   # output channel only
uv run sinbosen-la548 set-eq-band <ip> 0 0 --freq 45 --q 1.0 --type 4 --slope 0 --enable   # AllPass at 45 Hz, gain auto-zeroed
uv run sinbosen-la548 eq-bypass <ip> 0 on               # exits locally as unsupported; sends no packet
uv run sinbosen-la548 eq-reset <ip> 0                   # exits locally as unsupported; reset bands individually
uv run sinbosen-la548 set-dynamic-eq-band <ip> 0 0 --freq 100 --gain 6 --q 2 --type 0 --slope 0 --startup-mode 0 --start-time 1 --release-time 100 --ratio 4 --threshold -20 --enable  # input-only
uv run sinbosen-la548 set-compressor <ip> 0 --start-time 1 --release-time 100 --ratio 4 --threshold -20 --enable  # input or --output
uv run sinbosen-la548 set-limiter <ip> 0 --start-time 1 --release-time 50 --threshold -6 --detection-type 0 --enable   # output-only
uv run sinbosen-la548 set-expander <ip> 0 --start-time 1 --release-time 100 --ratio 2 --threshold -50 --enable   # input-only
uv run sinbosen-la548 set-mix-route <ip> 0 2 on        # connect input 0 to output 2 ('on' = connected, confirmed live)
uv run sinbosen-la548 set-input-source <ip> 0 on --source 1 --freq 100 --gain -6.0  # input-only
uv run sinbosen-la548 set-fir <ip> 0 on --type 1 --win-type 0 --freq-low 80 --freq-high 8000 --atten 30 --order 1
uv run sinbosen-la548 set-freq-shifter <ip> 0 3 0 on   # level 3, direction 0 (input-only, see docs/PROTOCOL.md)
uv run sinbosen-la548 save-program <ip> 0              # save current live state to program slot 0
uv run sinbosen-la548 load-program <ip> 0              # OVERWRITES all live settings from program slot 0
uv run sinbosen-la548 read-fir-coefs <ip> 0 fir0.bin    # exact 2048-byte DA0408X channel backup
uv run sinbosen-la548 write-fir-coefs <ip> 0 fir0.bin   # restore and verify that backup; rolls back on mismatch
uv run sinbosen-la548 channel-copy <ip> 0 1 4           # copy input channel 0's settings onto input channel 1 (this unit: 4 inputs/8 outputs, confirmed - use 8 for --output)
uv run sinbosen-la548 diff-group before.bin after.bin  # locate a control's byte offset in a read_group dump
```

What the CLI guarantees:

- **`set-*` commands** print `acknowledged` when the device acks the frame.
  An ack only means the frame arrived (docs/PROTOCOL.md, "Transport safety
  model"). Add `--verify` to read settings back and compare the stored
  control with the exact bytes sent; a mismatch exits 1.
- **Values** outside the ranges the manufacturer's app allows are refused
  before anything is sent, e.g. volume -80..+12 dB or compressor ratio
  1..128 (docs/PROTOCOL.md, "Value bounds").
- **Backups:** `dump-group` and `read-fir-coefs` write atomically and never
  replace a good backup with an empty or invalid reply. `dump-group` saves
  only a reply that decodes as a DA0408X snapshot (`--no-validate` to
  override).
- **`write-fir-coefs`** refuses a locked device and backs up the current
  block first. If the readback doesn't match, it restores the backed-up
  block.
- **`diff-group`** exits like diff(1): 0 identical, 1 different (including a
  length mismatch), 2 when a file can't be read.

Or use it as a library:

```python
from sinbosen_la548.client import Client

with Client("192.168.1.100") as client:
    print(client.read_lock_state())
```

Typed settings readback is available for the fixture-matched **DA0408X**
snapshot layout (4 inputs, 8 outputs, 7 input EQ bands, 15 output EQ bands):

```python
from sinbosen_la548.client import Client

with Client("192.168.1.100") as client:
    settings = client.read_settings()
    print(settings.outputs[0].hp)
    print(settings.inputs[0].eq_bands[0])
    print(settings.mix_matrix[0][0])  # input A -> output 1
```

This API performs one read and no writes, rejects incompatible snapshots, and
preserves original bytes. The profile's 96 kHz sample rate is confirmed on
hardware but not read from the device. FIR parameters decode through
`fir_adapter.FIRParameters`. Each channel's 512-value FIR coefficient block
is what FIR type 5 applies, and backups keep it byte-exact. See [settings readback](docs/SETTINGS_READBACK.md).

## Local DSP Studio

```sh
uv sync --extra ui
uv run --extra ui sinbosen-la548-ui --demo
```

Open <http://127.0.0.1:7860>. The app starts with an offline fixture, sharing
disabled. Enter the DSP address and Connect to read its actual settings.
Crossovers, EQ/allpass, gain/delay/polarity, and the mixer have local previews;
**Apply changes** writes only pending controls and verifies readback. Optional
Live tuning starts off. Native OSM stored traces and text exports can overlay
the filter plots and support phase-aware alignment. Advanced controls, local
presets, downloads, program operations, and channel copy are included.
Selected-channel FIR coefficient backups can be downloaded and restored with
exact readback verification.

See the [UI guide](docs/UI_GUIDE.md) for tuning, trace formats, state behavior,
and model limitations. Automated offline checks pass; UI hardware acceptance
and OSM display comparison remain opt-in verification work.

## Development

```sh
uv run --extra ui pytest
uv run --extra ui ruff check .
uv run --extra ui mypy
```

Unit tests cover the pure protocol/struct-packing logic and run without any
hardware (`testpaths = ["tests"]` in `pyproject.toml`, so `uv run pytest`
never touches real hardware). `hardware_test/` is a separate
hardware-in-the-loop suite that validates the write API against a real unit
through an audio loopback: an audio interface plays test signals into the
DSP's inputs and records its outputs. See
[hardware_test/README.md](hardware_test/README.md) for wiring, configuration
(`LA548_HIL_*` environment variables), safety rules, and how to run it
(opt-in only, via `--run-hardware` plus a real `LA548_HOST`).

## Project layout

```
src/sinbosen_la548/
  protocol.py     # frame envelope pack/unpack, checksum, Opcode enum
  discovery.py    # CH9120 UDP broadcast search (best-effort, see docs/PROTOCOL.md)
  transport.py    # UDP socket wrapper, HandShake keepalive, request/reply matching
  client.py       # high-level API: connect, read_lock_state, get_user_name, read_group,
                  #   set_mute/volume/phase/delay/xover/eq_band (EQ-all methods fail locally),
                  #   set_dynamic_eq_band, set_compressor/limiter/expander, set_mix_route,
                  #   read_mix_matrix/write_mix_matrix, set_input_source, set_fir,
                  #   set_freq_shifter, save_program, load_program, read_fir_coefs, channel_copy
  registry.py     # ParamSpec table - the only source of truth for a control's opcode/header fields
  codecs.py       # payload encode/decode for each control's wire format
  blobdiff.py     # diff two read_group() dumps to locate a control's byte offset
  cli.py          # `sinbosen-la548` command-line entry point
docs/
  PROTOCOL.md           # protocol reference
  SETTINGS_READBACK.md  # typed settings snapshot layout
  FILTER_MODELS.md      # measured filter models behind the response previews
  UI_GUIDE.md           # DSP Studio web UI guide
hardware_test/
  Hardware-in-the-loop test suite (audio loopback through a real unit) - see its own README
```

## License

MIT; see [LICENSE](LICENSE).
