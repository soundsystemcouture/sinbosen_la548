# hardware_test — hardware-in-the-loop tests

These tests check `sinbosen_la548`'s write API against a real LA548 by
measuring audio, not just ACKs. An audio interface acts as a pure electrical
loopback: each test plays a signal out of the interface, through the LA548,
back into the interface, and measures what comes back in Python.

**This directory never runs in the default test gate.** `pyproject.toml` sets
`testpaths = ["tests"]`, so `uv run pytest -q` never collects it. On top of
that, `hardware_test/conftest.py` skips every test here unless both
`--run-hardware` and `LA548_HOST` are given, so nothing touches a device by
accident.

## Requirements

- An LA548 on the network. Its IP is shown on the front panel.
- A Linux host running PipeWire, with `pw-cat`, `pw-record` and `pactl`.
- An audio interface (USB or other) with at least two outputs and two
  inputs, and playback and capture on one clock (normally the same device).
  Delay and phase measurements depend on the shared clock.
- The `ui` extra plus the `hardware` and `dev` dependency groups (the tests
  need numpy and scipy).

## Wiring

- Interface output 1 → LA548 input **A**
- Interface output 2 → LA548 input **B**
- LA548 **output 1** → interface input 1
- LA548 **output 2** → interface input 2

"Output 1/2" and "input 1/2" here mean the first two channels named in
`LA548_HIL_PLAYBACK_CHANNELS` and `LA548_HIL_CAPTURE_CHANNELS`. Use
line-level inputs on the interface. Turn off any direct monitoring or
input-to-output routing on the interface, which would feed the LA548's outputs
back into its inputs.

Input channel_id 0/1 is physical input A/B, and output channel_id 0/1 is
physical output 1/2. `test_00_rig_and_mute.py` confirms this by muting one
channel at a time and watching which capture channel goes quiet. The tests
also use the mix matrix: `Rig.reset_both_channels_safe()` routes A → output 1
and B → output 2, and disconnects every other route from A and B.

## Configuration

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `LA548_HOST` | yes | | The LA548's IP address |
| `LA548_HIL_SINK` | yes | | PipeWire playback node name |
| `LA548_HIL_SOURCE` | yes | | PipeWire capture node name |
| `LA548_HIL_PLAYBACK_CHANNELS` | no | `FL,FR` | Comma-separated channel names, at least two. The first two drive LA548 inputs A/B; any others play silence |
| `LA548_HIL_CAPTURE_CHANNELS` | no | `FL,FR` | Comma-separated channel names, at least two. The first two record LA548 outputs 1/2 |
| `LA548_HIL_RATE` | no | `48000` | Sample rate for playback and capture |
| `LA548_HIL_CARD` | with `LA548_HIL_PROFILE` | | pactl card whose profile to select before each test |
| `LA548_HIL_PROFILE` | with `LA548_HIL_CARD` | | Profile to select on that card, e.g. `pro-audio` |
| `LA548_SCRATCH_PROGRAM_SLOT` | `test_a6` only | | A program slot (0-29) that `test_a6` may overwrite |

Missing or invalid `LA548_HIL_*` variables make each test error at rig
setup, with one message naming every bad variable (`hardware_test/loopback.py`).

To find the names:

```sh
pactl list short sinks      # playback node names, for LA548_HIL_SINK
pactl list short sources    # capture node names, for LA548_HIL_SOURCE
pactl list short cards      # card names, for LA548_HIL_CARD
pw-cli ls Port              # port names such as playback_FL or capture_AUX2
```

Channel names are the part of a port name after `playback_` or `capture_`
(`FL`, `FR`, `AUX0`, ...). `pactl list sinks` and `pactl list sources` show the
same channel map in long form (`front-left`, `aux0`). Use the short form.

Many USB interfaces only expose flat, correctly ordered `AUXn` channels under
PipeWire's `pro-audio` card profile, and a default profile may remap or drop
channels. If yours does, set `LA548_HIL_CARD` and
`LA548_HIL_PROFILE=pro-audio`. The rig then switches to that profile before
each test and checks that the switch took.

It is convenient to keep the variables in an env file outside the repository
and load it with `set -a; . path/to/hil.env; set +a`.

## Running

Run **one file at a time**, starting with `test_00`, rather than the whole
directory unattended:

```sh
LA548_HOST=<device-ip> uv run --extra ui --group hardware --group dev \
  pytest hardware_test/test_00_rig_and_mute.py --run-hardware -s -v
```

Swap in each file name from the coverage table below. `-s` shows each test's
printed measurements.

Type-check this directory separately; the project's `mypy` gate covers only
`src`:

```sh
uv run --extra ui --group hardware --group dev mypy hardware_test
```

Raw captures, if a test saves any, go in `hardware_test/captures/`, which is
gitignored.

## Safety rules

- Never connect the LA548's outputs to an amplifier or speakers while these
  tests run. They apply EQ and Dynamic EQ boosts of up to +15 dB.
- Check for leftover device state before a session. Dump the active group
  with `uv run sinbosen-la548 dump-group <device-ip> before.bin` and compare it
  with a known-good dump using `uv run sinbosen-la548 diff-group`. A leftover
  from an interrupted run makes later tests fail for the wrong reason. For
  example, output channel 0's EQ band 0 left enabled at 1 kHz/+15 dB/Q 2 pushed
  `test_20`'s correlation below its threshold. After a session, dump again
  and diff. A full run leaves the 2464-byte group dump identical to the
  pre-run one.
- Power-cycle the LA548 before trusting a null result. This DSP can be
  wedged by malformed packets: storage and ACKs keep working, but audio stops
  responding. Dynamic EQ once measured no effect at all until a power cycle,
  and an output once read silent with routing, mute and processing all
  correct. If a write reaches storage but isn't audible, or an output reads
  silent, reboot and re-run before concluding anything.
- Change one control at a time. Treat the first send of any new opcode or
  field as an experiment, and verify it with a `read_group()` diff and a
  measurement, since an ACK alone doesn't prove the write applied.
- Never send opcodes `0xFA`/`0xFD`/`0xFE`/`0xFF`. Their purpose is unknown
  and they may be destructive. The transport refuses them. `write_group()`
  is deliberately not implemented.

## The restore fixture

Every test that uses the `rig` fixture starts from a `read_settings()`
snapshot. Pass or fail, `restore.restore_settings` re-writes any control that
changed and checks the readback, so a failure part-way through can't leave
EQ, crossover, dynamics, FIR parameters or input-source changes behind for
later tests. A restore that doesn't verify is reported as a test error.

FIR coefficient blocks and program slots are outside `read_settings()` and
are **not** restored. Back up FIR coefficients first with
`uv run sinbosen-la548 read-fir-coefs` if you need them. After a full run
they were unchanged.

## Program slots (`test_a6`)

`test_a6_program_slots.py` saves to a program slot, and a slot's name
doesn't show whether it holds a saved program, because saving never renames
it. The test is skipped unless you name a slot it may overwrite:

```sh
LA548_SCRATCH_PROGRAM_SLOT=<0-29> LA548_HOST=<device-ip> uv run --extra ui --group hardware --group dev \
  pytest hardware_test/test_a6_program_slots.py --run-hardware -s -v
```

The slot keeps the test's saved state afterwards. `load_program` overwrites
every channel's live settings. The restore fixture puts the
`read_settings()` controls back afterwards.

## Coverage

| File | Verifies | Status |
| --- | --- | --- |
| `test_00_rig_and_mute.py` | The chain carries a tone on both capture channels; input and output mute silence exactly one channel; channel_id ↔ physical port mapping | Passes |
| `test_10_volume.py` | Input and output volume track the configured dB within 0.5 dB, without moving the other channel | Passes; every 5 dB step measured 5.0 dB |
| `test_20_phase.py` | Inverting phase flips the correlation sign of exactly one capture channel, for both input and both output channels | Passes |
| `test_30_delay.py` | Input and output delay track the configured ms within 0.5 ms, from 0 to 1000 ms, across the codec's 49-50 ms boundary | Passes; worst error 0.15 ms, at 1000 ms |
| `test_40_xover.py` | Output HP/LP corners land within one octave of the configured frequency; 24 dB/oct attenuates more than 6 dB/oct | Passes |
| `test_50_eq_band.py` | ±12 dB boost/cut within 1 dB on input and output; `test_52` fits Q to the RBJ model | Passes; fitted Q 2.01 vs stored 2.012 |
| `test_60_allpass.py` | First- and second-order allpass phase matches the RBJ model within 5° at 200, 1000 and 1500 Hz centres, with level flat within 1 dB | Passes; worst-case phase error 0.4° |
| `test_70_eq_bypass_polarity.py` | `eq_all_bypass` (opcode 0xAF) | Skipped by design: `eq_all_bypass` has no effect on this firmware and the client refuses it |
| `test_71_eq_bypass_fallback.py` | Disabling every band returns a boosted channel to within 1 dB of flat | Passes |
| `test_72_eq_all_reset.py` | `eq_all_reset` (opcode 0xB0) | Skipped by design: `eq_all_reset` has no effect on this firmware and the client refuses it |
| `test_80_mix_route_polarity.py` | `set_mix_route(..., True)` connects a crosspoint and `False` disconnects it | Passes |
| `test_90_dyneq_wire_format.py` | A Dynamic EQ whole-struct write reaches device storage (`read_group()` diff) | Passes |
| `test_91_dyneq_audible_effect.py` | A Dynamic EQ boost is audible (more than 10 dB) on all three bands | Passes. Power-cycle first if it reads flat |
| `test_a0_dynamics_wire_format.py` | Output compressor, limiter and expander writes reach device storage | Passes. The input-compressor case is skipped by design: input compressor has no effect on this firmware and the client refuses it |
| `test_a1_dynamics_audible_effect.py` | Compressor, limiter and expander each reduce a tone's level by more than 10 dB | Passes |
| `test_a2_input_source.py` | `source_select=1` is an internal sine generator at `freq_hz`; `enabled` and `gain_db` gate and trim an internal generator | Passes |
| `test_a3_fir.py` | FIR `enabled` toggles filtering; the low-pass corner is `freq_low_hz` and the high-pass corner is `freq_high_hz` | Passes |
| `test_a4_freq_shifter.py` | Frequency shifter: `enabled` shifts the tone, `direction` sets the sign (0 down, 1 up), `freq_level` scales the shift | Passes |
| `test_a5_channel_copy.py` | `channel_copy` copies a channel's EQ onto the target, input and output | Passes |
| `test_a6_program_slots.py` | `save_program`/`load_program` round-trip a distinctive EQ state | Skipped unless `LA548_SCRATCH_PROGRAM_SLOT` is set. Passes on hardware: the reloaded level was within 0.1 dB of the saved one |
| `test_a7_fir_win_type.py` | `win_type` selects distinct FIR shapes: Blackman-Harris stays measurably less attenuated than Hanning/Hamming just above the corner. The Kaiser/attenuation comparison is report-only | Passes |
| `test_a8_fir_order.py` | `order_index` 0 and 1 both give a working low-pass near the configured corner. Steepness is reported, not graded | Passes; index 0 is measurably less steep than 1 |
