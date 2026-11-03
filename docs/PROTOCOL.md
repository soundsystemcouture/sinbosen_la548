# LA548 network protocol reference

This is the wire-level specification implemented by `src/sinbosen_la548/`
for the Sinbosen LA548 network audio DSP (4 inputs, 8 outputs). It covers
the frame format, transport behaviour, the opcode and parameter-ID
catalog, the payload layout and encoding of every implemented control,
whole-device operations, and known device quirks.

Evidence is labelled throughout:

- **Confirmed on hardware**: measured against a real LA548, normally by a
  test in `hardware_test/` (named where it applies).
- **Confirmed by readback**: a `read_group()` (0xA9) snapshot taken after
  the write shows exactly the expected stored bytes.
- **Observed**: seen on the device, but not turned into a repeatable test.
- **Not confirmed on hardware** / **unverified**: not checked against the
  device.

An acknowledgement from the device is never treated as evidence that a
write took effect (see "Transport safety model").

## Overview

Everything uses **UDP port 8087**. Two unrelated sub-protocols share that
port:

1. The **CH9120 discovery/config protocol**, spoken by the WCH CH9120
   serial-to-Ethernet bridge chip in the unit's network interface
   (`discovery.py`). It has not been made to work on this hardware; see
   "CH9120 discovery/config protocol".
2. The **application command protocol** implemented by the DSP firmware
   (`protocol.py`, `transport.py`, `client.py`). The rest of this document
   describes it.

The device answers opcode 0xD9 with the model string `DA0408X` (see
"ReadDeviceId (0xD9)"). The client's settings profile for this model has 4
inputs (A-D) and 8 outputs (1-8), 7 EQ bands per input, 15 per output, and
3 Dynamic EQ bands per input. Channel IDs on the wire are 0-based:
`channel_id` 0 is input A or output 1. The packed settings layout is in
[SETTINGS_READBACK.md](SETTINGS_READBACK.md).

## Frame format

Requests and replies have the same shape:

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0-1 | 2 | `0x5A 0x5A` | sync |
| 2 | 1 | linkage | link-group mask (see below); the client always sends 0 |
| 3 | 1 | device_number | echoed in replies |
| 4 | 1 | version / machine model | requests use `10` (0x0A). Replies are opcode-dependent: `10` on normal reads, `19` (0x13) on HandShake (0xA1) and 0xD9 replies (observed) |
| 5 | 1 | **opcode** | see "Opcodes" |
| 6 | 1 | channel_type | 0 = input, 1 = output, 2 = mix matrix, 3 = default |
| 7 | 1 | user_id | program slot for 0xAB/0xAC; 0 otherwise |
| 8 | 1 | channel_id | 0-based channel a write addresses. **Meaningful for 0xA4**, never a free default (see "Unsafe opcodes") |
| 9 | 1 | module_id | processing block a 0xA4 write addresses. **Meaningful for 0xA4** |
| 10 | 1 | data_id | sub-selector within a module; see "Parameter IDs" |
| 11-12 | 2 | payload length N, little-endian | |
| 13 … 13+N-1 | N | payload | opcode-specific; multi-byte integers are little-endian |
| 13+N | 1 | checksum | `sum(bytes[0:13+N]) % 256` |
| 14+N | 1 | `0xAA` | tail |

Example: the empty 0xD9 request is `5a5a00000ad90000000000000097aa`.

The device does not appear to check the checksum on receipt (unverified);
the client always computes it correctly.

**Linkage (byte 2)** is a bit-packed mask that mirrors a write to the other
channels of a link group. Its semantics and group mapping have not been
confirmed on hardware. The client always sends 0, so a write never reaches
a channel the caller did not address.

### Receive validation

`protocol.unpack_frames` resyncs on `0x5A 0x5A` and cuts frames using the
length field. It accepts a candidate frame only if its tail is `0xAA`, its
checksum matches, and its length field is at most `protocol.MAX_PAYLOAD`
(4096). Anything else is treated as a false sync match: the scanner skips
one byte and resyncs on the next `5A 5A`, so one corrupt or spoofed frame
can't swallow the frames behind it. The length check matters most: a false
sync claiming 0xFFFF bytes would otherwise stall every later reply until
64 KiB arrived. `Transport.rejected_frames` counts rejects by reason, and
`Transport.checksum_errors` is the checksum count.

Checksum enforcement was checked offline against recorded packet captures.
All 11,033 device-originated frames validate, across nine reply opcodes
(0xA1, 0xA4, 0xA9, 0xB6, 0xC8, 0xC9, 0xCA, 0xCB, 0xD9). The largest payload
seen was the 2464-byte ReadGroup reply. Confirmed on hardware: a live
session of status, settings-snapshot and read-loop traffic ended with
`checksum_errors == 0` and no rejected frames.

## Transport

### Transport safety model: an ack is not proof

The protocol has no per-message nonce, and the device keeps one global
peer endpoint (see "ReadDeviceId (0xD9)"). `Transport.request` matches a
reply on opcode plus every header field the request set (`channel_type`,
`user_id`, `channel_id`, `module_id`, `data_id`). `version` is excluded
because replies use an opcode-dependent value. Confirmed on hardware: the
device echoes those header fields on 0xA4 acks for every implemented
control. The exception is 0xB6, matched by opcode only (see "Opcodes").

After a timeout the transport does the following:

- **Quarantine.** The request's reply signature is quarantined for
  `QUARANTINE_S` (3 s, about twice the 1.05 s by which the slowest measured
  straggler outlasted its timeout). A later request whose reply could match
  that signature first waits the quarantine out, then drains the inbox, so
  a late reply can't be taken for its own. Requests that can't match
  proceed immediately.
- **Buffer reset.** The receive buffer's partial frame is discarded.

Switching to a fresh local port does not help: late replies follow the
newest port (see "ReadDeviceId (0xD9)").

Two cases are still not excluded at this layer:

- a duplicate reply (observed: two 0xB6 frames for one request) that
  arrives after the pre-send drain;
- a straggler slower than the quarantine.

So an ack means only that a frame was received. Readback is the proof that
a write applied: `DSPService.apply` re-reads and compares, and the CLI's
`set-* --verify` compares the stored control with the exact bytes sent.
Stored state updates synchronously: a `read_group()` immediately after a
write's ack already shows the new value (confirmed by readback for
polarity writes).

Because the device has one peer endpoint and one send queue, concurrent
controllers can steal or duplicate one another's replies even when each
uses its own local port. Don't run two controllers against one unit at
once.

The transport also has these limits and guards:

- It is single-use: `start()` after `stop()` raises, and a new `Client` is
  needed to reconnect.
- It resolves hostnames once, so `localhost` works for both `sendto` and
  the source filter.
- `Transport.send` refuses 0xFA/0xFD/0xFE/0xFF and any int that isn't a
  known `protocol.Opcode`.
- `Client._write_param` refuses the registry's `BLOCKED` specs (EQ
  all-bypass and all-reset).

### Minimum gap between frames

**Confirmed on hardware.** The device silently drops a frame that arrives
within a few milliseconds of the previous one. Two requests sent
back-to-back reliably lose one reply; a gap of 50 ms or more reliably
fixes it. `transport.py` enforces a `MIN_SEND_GAP` of 60 ms across *all*
sends (explicit requests and the keepalive loop alike) via a shared lock,
so callers don't need to think about it. `Client.connect()` also starts
the keepalive loop only after the initial request has completed, for the
same reason.

### Keepalive and meter levels

The client sends `HandShake` (0xA1) every **180 ms** (`HANDSHAKE_INTERVAL`)
and treats the link as down after **6 s** without a reply
(`LINK_TIMEOUT`). See `Transport.start_keepalive()` and
`Transport.link_up`.

The request is empty. The reply carries a ~60-byte payload that decodes as
a run of little-endian float32 values; the repeated value seen at rest
(`00 00 48 c3`) is exactly **-200.0**, a usual metering silence floor. The
payload is therefore taken to be per-channel signal-meter levels. The
per-channel byte offsets are not mapped.

### Connect sequence

`Client.connect()` sends one `ReadLockState` (0xB6) with an empty payload
and all-default header fields. Two other side-effect-free reads are useful
right after connecting, and are separate client methods:

1. `ReadLockState` (0xB6): empty payload, all-default header fields.
2. `GetUserName` (0xCA): empty payload, all-default header fields
   (`get_program_names()`).
3. `ReadGroup` (0xA9): empty payload, `user_id=0` (`read_group()`,
   `read_settings()`).

No dedicated session or "hello" opcode is known. Any of these reads
establishes the link, and once the device has replied it also answers the
keepalive.

## Opcodes

The constant column is the client's `protocol.Opcode` member.

| Opcode | Constant | Purpose | Status | Notes |
|---|---|---|---|---|
| 0xA1 | `HAND_SHAKE` | keepalive / meter poll | **Confirmed on hardware** | empty request; ~60-byte meter reply (see "Keepalive and meter levels") |
| 0xB6 | `READ_LOCK_STATE` | panel lock state | **Confirmed on hardware** | reply is 5 bytes: locked flag + 4 ASCII-digit PIN, e.g. `b"\x000000"` = `locked=False, pin="0000"`. The firmware has replied with `channel_id=1` to the all-zero-header request, so the client matches this reply by opcode only |
| 0xCA | `GET_USER_NAME` | program-slot names | **Confirmed on hardware** | 750 bytes: 30 program names as 25-byte NUL-padded UTF-8 slots. `get_program_names()`; `get_user_name()` is a slot-0 alias |
| 0xA9 | `READ_GROUP` | settings snapshot | **Confirmed on hardware** | request `user_id=0`. Reply is 2464 bytes: 2164 packed settings bytes plus twelve 25-byte channel names. `read_settings()` decodes the DA0408X layout; other models' layouts are not supported |
| 0xA4 | `WRITE_MODULE_ID` | parameter write | **Confirmed on hardware** for every control in "Implemented controls" | the one generic write for every per-channel control; its meaning is set entirely by `channel_type`/`channel_id`/`module_id`/`data_id`/payload. **Default header fields are a real write** (see "Unsafe opcodes"). Only ever built through the registry (see "Write path") |
| 0xAF | `EQ_ALL_BYPASS` | bypass all EQ bands of a channel | **Refused by the client**: no reply, no effect | `hardware_test/test_70_eq_bypass_polarity.py`; see "EQ all-bypass and all-reset" |
| 0xB0 | `EQ_ALL_RESET` | reset all EQ bands of a channel | **Refused by the client**: acks, changes nothing | `hardware_test/test_72_eq_all_reset.py` |
| 0xA8 | `WRITE_GROUP` | write settings snapshot | **Not implemented** | see "Not implemented" |
| 0xD9 | `READ_DEVICE_ID` | model string | **Refused by the client** after one controlled trial | slow, model-specific, disturbs request sequencing; see "ReadDeviceId (0xD9)" |
| 0xC8 | `READ_FIR_COEFS` | read one channel's FIR coefficients | **Confirmed on hardware** | empty request; 2048-byte reply = 512 float32 LE values (`Client.read_fir_coefs`) |
| 0xC9 | `WRITE_FIR_COEFS` | write one channel's FIR coefficients | **Confirmed by readback** | one complete 2048-byte block per channel; changed-byte readback and exact restoration confirmed on input B and output 2 (`Client.write_fir_coefs`) |
| 0xAB | `DEVICE_PROGRAM_SAVE` | save live state to a program slot | **Confirmed on hardware** | `hardware_test/test_a6_program_slots.py`; see "Whole-device operations" |
| 0xAC | `DEVICE_PROGRAM_READ` | load a program slot | **Confirmed on hardware** | **overwrites all live device state**; `test_a6_program_slots.py` |
| 0xB5 | `CHANNEL_COPY` | copy one channel's settings to another | **Confirmed on hardware**, both directions | `hardware_test/test_a5_channel_copy.py` |
| 0xCB | `SET_USER_NAME` | rename a program slot | **Refused by the client** | corrupts hidden slot bytes; see "SetUserName (0xCB)" |
| 0xB1 | `DATA_RESET` | factory reset | **Not implemented** (deliberately) | |
| 0xAD | `FIRMWARE_UPDATE` | firmware update | **Not implemented** | out of scope |
| 0xB8 | `CLEAR_PASSWORD` | clear password | **Not implemented** | not needed on a trusted network |
| 0xD8 | `CHANGE_PASSWORD` | change password | **Not implemented** | not needed on a trusted network |
| 0xC5 | `SET_RESTORE` | unknown | **Not implemented** | payload not decoded |
| 0xFA | `UPDATE_FA` | unknown | **Never send** | purpose unknown and possibly destructive; `Transport.send` refuses it |
| 0xFD | `UPDATE_FD` | unknown | **Never send** | same |
| 0xFE | `UPDATE_FE` | unknown | **Never send** | same |
| 0xFF | `UPDATE_FF` | unknown | **Never send** | same |

These further opcode numbers are known only by a descriptive name. Nothing
about their payloads or effects is known, they are not in
`protocol.Opcode`, and `Transport.send` refuses them. Do not send them
speculatively.

| Opcode | Name |
|---|---|
| 0xC0 | speaker check |
| 0xC1 | set device ID over TCP/IP |
| 0xC2 | set speaker frequency range |
| 0xC3 | IIR/FIR change |
| 0xC4 | set source |
| 0xC6 | clear feedback-suppressor filters |
| 0xC7 | read feedback-suppressor filters |

## Parameter IDs

A 0xA4 write is addressed by `channel_type`, `channel_id`, `module_id` and
`data_id`. The tables below list the complete known ID sets under this
client's descriptive names. Only the modules with a client control (see
"Implemented controls") have been exercised on hardware.

### Channel types

`registry.ChannelType`:

| Value | Name | Use |
|---|---|---|
| 0 | `IN` | input channels |
| 1 | `OUT` | output channels |
| 2 | `MIX` | mix-matrix crosspoints |
| 3 | `DEFAULT` | not used by any implemented write |

### Module IDs

| module_id | Block | Client control |
|---|---|---|
| 0 | mute | `set_mute` |
| 1 | gain (volume) | `set_volume` |
| 2 | polarity | `set_phase` |
| 3 | delay | `set_delay` |
| 4-34 | EQ bands 1-31 (`4 + band_index`) | `set_eq_band` |
| 35-37 | Dynamic EQ bands 1-3 (`35 + band_index`) | `set_dynamic_eq_band` |
| 38 | crossover high-pass | `set_xover(..., "hp", ...)` |
| 39 | crossover low-pass | `set_xover(..., "lp", ...)` |
| 40 | compressor | `set_compressor` |
| 41 | limiter | `set_limiter` |
| 42 | expander | `set_expander` |
| 43 | input source (signal generator) | `set_input_source` |
| 44 | reverb | none |
| 45 | automatic gain control | none |
| 46 | noise gate | none |
| 47 | feedback suppressor | none |
| 48 | frequency shifter | `set_freq_shifter` |
| 49 | FIR filter parameters | `set_fir` |
| 50 | module enable | none |
| 51 | mixer level | none (no level write is known) |
| 52 | feedback-suppressor gain | none |
| 53 | system | none |

Mix-matrix crosspoints use `channel_type=2` with `module_id=0` (see "Mix
matrix").

### Data IDs

Every implemented block write sends the whole block with `data_id=0`. The
basic controls are the exception: mute, gain, polarity and delay use
`data_id` equal to their `module_id` (0, 1, 2, 3). The per-field data IDs
below exist, but per-field frames are not how the device takes writes:
for crossover (data IDs 38-41) and EQ (49-54), per-field frames were
confirmed on hardware to be acknowledged with no effect on the audio. For
the other blocks only the whole-block write is used or tested.

| data_id | Field |
|---|---|
| 0 | base: mute |
| 1 | base: gain |
| 2 | base: polarity |
| 3 | base: delay |
| 4 | base: source |
| 5-8 | input source: enable, source select, gain, frequency |
| 9-13 | noise gate: enable, attack, release, ratio, threshold |
| 14-18 | compressor: enable, attack, release, ratio, threshold |
| 19-24 | limiter: enable, attack, release, ratio, threshold, detection type |
| 25-29 | expander: enable, attack, release, ratio, threshold |
| 30-37 | reverb: enable, direct level, level, time, damping, room, wet, pre-delay |
| 38-41 | crossover: enable, type, slope, frequency |
| 42-48 | FIR: enable, type, window, high frequency, low frequency, beta (attenuation), order |
| 49-54 | EQ: enable, type, slope, Q, level, frequency |
| 55-65 | Dynamic EQ: enable, type, slope, mode, Q, level, frequency, attack, release, ratio, threshold |
| 66-68 | frequency shifter: enable, level, direction |
| 69-70 | denoise: enable, level |
| 71-72 | automatic gain control: enable, threshold |
| 73-81 | feedback suppressor: enable, sense mode, fixed-filter count, gain, learn mode, clear filters, set filter, release on, release time |
| 82 | mixer value |
| 83 | module display |

### FIR enumerations

FIR filter type (`set_fir(fir_type=...)`): 0 = low-pass, 1 = high-pass,
2 = band-pass, 3 = band-stop, 4 = all-pass. Only 0 and 1 have been tested
on hardware.

FIR window type (`set_fir(win_type=...)`), in the order the manufacturer's
app lists them: 0 = Kaiser, 1 = Hanning, 2 = Hamming, 3 = Blackman,
4 = Flat-top, 5 = Blackman-Harris, 6 = Blackman-Nuttall, 7 = Nuttall,
8 = Kaiser-Bessel, 9 = Trapezoid, 10 = Gauss, 11 = Sine. The client assumes
raw value = list position; see "FIR" for what hardware confirms.

## Unsafe opcodes

### 0xA4 with default header fields mutes input 1

0xA4 is a write, not a handshake. A frame with every header field at its
default (`channel_type=0`, `channel_id=0`, `module_id=0`, `data_id=0`) and
a 1-byte payload of `0x00` is a well-formed "set input 1 mute" write, and
mute uses an inverted encoding where `0x00` means **muted**. Observed on
the device: sending this frame on every connect muted input 1 each time.

Rules that follow from this:

- Never send 0xA4 with guessed or default header fields, and never build
  its header fields by hand at a call site. Every 0xA4 frame comes from a
  `registry.ParamSpec` through `Client._write_param` (see "Write path").
- `Client.connect()` sends only `ReadLockState` (0xB6).
- `protocol.encode_frame` raises `FrameError` for an out-of-range header
  byte instead of masking it with `& 0xFF`, so a caller bug can't wrap
  into a different, well-formed frame aimed at another control.
- Band indices are range-checked because module IDs are contiguous: EQ
  band 31 would land on Dynamic EQ band 1 (module 35) and EQ band -1 on
  delay (module 3); Dynamic EQ band 5 would land on the compressor
  (module 40) and band -1 on EQ band 31 (module 34).

### 0xFA, 0xFD, 0xFE, 0xFF: never send

Their purpose and payloads are unknown, and they could be destructive, so
they must not be probed blind.
`Transport.send` refuses them (`protocol.UNKNOWN_DANGEROUS_OPCODES`).

### ReadDeviceId (0xD9)

`Client.read_device_id()` raises before transmitting, and the matching
hardware stage is disabled. One controlled trial established why.

A single empty request (`5a5a00000ad90000000000000097aa`) got no reply
within a deliberately short 0.5 s timeout. The client then sent an
ordinary 0xB6 lock-state read from a fresh local port. While the delayed
operation was in progress the device emitted many tiny UDP fragments.
1.548 s after the request, the **fresh** port, not the one that sent 0xD9,
received a complete, checksum-valid 0xD9 frame with an 11-byte payload:

```
5a5a000044413034303858
```

The last seven bytes are ASCII `DA0408X`, the model family. The first four
bytes are not an IPv4 address (read as one they give the nonsensical
`90.90.0.0`), so a generic "device ID = IP" parser is wrong for this
firmware. The reply used version byte `0x13` although the request used
`0x0A`, like HandShake replies.

The fresh socket also received queued HandShake replies caused by requests
on the older socket, plus two valid 0xB6 frames for its single lock
request. This is direct evidence that the device has one global peer
endpoint and send queue, not isolated UDP sessions. The lock read sharing
the fresh port timed out while 0xD9 was still occupying the reply path; a
separate fresh socket then got a lock-state reply about 74 ms after its
request.

Throughout the trial the device stayed healthy: the keepalive socket
received 140 valid handshakes, the unit answered every ping, and quiet
audio was continuous (20 ms RMS varied by at most 0.0114 dB). So 0xD9 is a
slow, endpoint-confusing command, not a cause of outages. Because its
payload is model-specific and its delayed reply disturbs request
sequencing, the client does not send it.

### SetUserName (0xCB)

0xCB accepts a 16-byte UTF-8 program name, but the firmware stores names
in 25-byte slots (see 0xCA). A controlled trial that renamed a slot and
then restored the original visible name left bytes 16-23 of that slot
changed. No write layout that restores a slot exactly is known, so
`set_program_name()` raises and sends nothing.

### EQ all-bypass and all-reset

0xAF and 0xB0 have no useful effect on this firmware, and the client
refuses both; see "EQ all-bypass and all-reset (0xAF, 0xB0)".

## Write path: the control API (`registry.py`, `codecs.py`, `Client._write_param`)

### Design

- Every 0xA4 write is a `registry.ParamSpec`: opcode, `channel_type`,
  `module_id`, `data_id` and a codec, fixed per control. The caller
  supplies only `channel_id` and the value.
- `Client._write_param(spec, channel_id, value)` is the only method that
  turns a `ParamSpec` into a frame. It always sends `linkage=0`, checks
  `channel_id` against the model's 4 inputs / 8 outputs, checks `user_id`,
  and refuses the `registry.BLOCKED` specs.
- Each registry entry is confirmed on hardware or by readback; the test is
  named in "Implemented controls".
- Every multi-field block is sent **whole**, in one frame, with
  `data_id=0`. Callers must supply every field on every call; there is no
  partial update. (Per-field frames are acknowledged but have no effect;
  see "Data IDs".)
- `codecs.py` encoders validate their inputs and raise `ValueError` rather
  than clamp (see "Value bounds"). Decoders stay permissive.

Client-side guards:

- `_write_param` bounds-checks `channel_id` and `user_id`, so an
  out-of-range program slot can't wrap onto slot 0.
- `eq_band_struct_spec` accepts band indices 0-30 (modules 4-34) and
  `dyneq_struct_spec` 0-2 (modules 35-37); `set_eq_band` further limits
  bands to the model's 7 input / 15 output bands.
- Each block is completely encoded and validated before anything is sent,
  so a bad field can't leave an earlier field already applied.
- `codecs.xover_slope_u8` requires an exact multiple of 6 dB/oct rather
  than rounding to the nearest step.
- `codecs.eq_q_u16` uses `floor()`, so a Q above the encodable maximum
  (~28.85) is rejected instead of truncating toward zero into raw 0.
- Offset-then-scale encoders (`volume_u16`, `eq_gain_u16`, `delay_u16`,
  `dyneq_starttime_u16`, `dyneq_ratio_u16`, input-source gain, FIR
  attenuation) round to 6 decimal places before their truncating cast, so
  binary float error can't drop a whole step (`1.15 * 100` is
  `114.99999999999999`, which would otherwise encode 1.15 ms of delay as
  1.14 ms).
- `channel_copy` validates the source and target channels.
- `dump-group` and `read-fir-coefs` refuse to write an empty or short reply
  (exit 1), so a failed read never overwrites a good backup.

### Implemented controls

Every control below is exposed as a `Client` method and a CLI subcommand,
and has been exercised on hardware.

| Control | module_id | data_id | channel_type | Payload | Client method | Evidence |
|---|---|---|---|---|---|---|
| Mute | 0 | 0 | IN / OUT | u8, **0x00 = muted, 0x01 = unmuted (inverted)** | `set_mute` | `test_00_rig_and_mute.py` |
| Volume | 1 | 1 | IN / OUT | u16, `raw = int((dB + 80) * 10)` | `set_volume` | `test_10_volume.py` |
| Polarity | 2 | 2 | IN / OUT | u8, 0 = normal, 1 = inverted (direction: see below) | `set_phase` | `test_20_phase.py` |
| Delay | 3 | 3 | IN / OUT | u16, piecewise (see "Delay") | `set_delay` | `test_30_delay.py` |
| Crossover HP / LP | 38 / 39 | 0 | OUT only | 5-byte block | `set_xover` | `test_40_xover.py` |
| EQ band | 4 + band | 0 | IN / OUT | 9-byte block | `set_eq_band` | `test_50_eq_band.py`, `test_60_allpass.py`, `test_71_eq_bypass_fallback.py` |
| Dynamic EQ band | 35 + band | 0 | IN only | 18-byte block | `set_dynamic_eq_band` | `test_90_dyneq_wire_format.py`, `test_91_dyneq_audible_effect.py` |
| Compressor | 40 | 0 | OUT (IN refused) | 10-byte block | `set_compressor` | `test_a0_dynamics_wire_format.py`, `test_a1_dynamics_audible_effect.py` |
| Limiter | 41 | 0 | OUT only | 10-byte block | `set_limiter` | same |
| Expander | 42 | 0 | IN only | 10-byte block | `set_expander` | same |
| Mix crosspoint | 0 | 0 | MIX | 3-byte block | `set_mix_route`, `write_mix_matrix` | `test_80_mix_route_polarity.py` |
| Input source | 43 | 0 | IN only | 6-byte block | `set_input_source` | `test_a2_input_source.py` |
| FIR parameters | 49 | 0 | IN / OUT | 9-byte block | `set_fir` | `test_a3_fir.py`, `test_a7_fir_win_type.py`, `test_a8_fir_order.py` |
| Frequency shifter | 48 | 0 | IN only | 3-byte block | `set_freq_shifter` | `test_a4_freq_shifter.py` |

All test files are under `hardware_test/`. `test_00_rig_and_mute.py` also
confirms the `channel_id`-to-physical-channel mapping in both directions.

### Mute, volume and polarity

`Client.set_mute(channel_id, muted, *, output=False)`,
`set_volume(channel_id, db, *, output=False)` and
`set_phase(channel_id, inverted, *, output=False)`, and the CLI commands
`set-mute`/`set-volume`/`set-phase` (each `ip channel [--output]` plus the
value). Without `output`/`--output` they address an input channel.

**Mute** uses the inverted encoding in the table: `0x00` is muted. A unit
test pins `encode(muted=True) == b"\x00"` so this can't silently flip.
Confirmed on hardware (`test_00_rig_and_mute.py`).

**Volume** is a u16, `raw = int((dB + 80) * 10)`, range -80 to +12 dB
(raw 0-920). The client truncates (for example -6.05 dB encodes as raw
739); at 0.1 dB steps this has not been distinguished from rounding on
hardware. The explicit floor matters, because truncation would otherwise
encode -80.09 dB as raw 0. `test_10_volume.py` confirms that measured
level changes track configured dB deltas. Behaviour above +12 dB has not
been probed (see "Value bounds").

**Polarity** is a u8; `codecs.phase_u8` maps `inverted=True` to 1 and
`False` to 0. Confirmed on hardware (`test_20_phase.py`, all four
channel/direction combinations, repeated across two clean power cycles):

- Input and output polarity are separate registers (input 1 at snapshot
  byte 1, output 1 at byte 613); writing one never touches the other.
- Each field independently and reliably flips the measured output sign
  (25 of 25 trials, both directions).
- The write is a "set", not a toggle: writing the same raw value five
  times keeps the stored byte steady.
- Longer settling (0.3 s to 1.0 s) changes nothing.
- With both polarity bytes at raw 0, the unit's output measured
  **inverted** relative to its input. The measurement interface was
  checked separately and does not invert.

Because of that end-to-end inversion, audio measurements cannot tell which
raw value is the 0° state: swapping the meaning of 0 and 1 for both the
input and the output field predicts the same four measured signs. **That 1
means "inverted" is not confirmed on hardware**; it follows the setting's
conventional meaning (0° / 180°). `test_20_phase.py` therefore asserts a
relative sign flip, not an absolute sign.

Once, a front-panel menu freeze during a test session was followed by
inconsistent polarity readings until a power cycle. Treat a wrong-sounding
polarity mid-session as a cue to power-cycle the unit, not as evidence the
encoding is wrong.

## Value bounds

The encoders in `codecs.py` raise `ValueError` outside the ranges the
manufacturer's app offers, so every path (CLI, presets, the Advanced
editor, `Client`) inherits them. The client refuses rather than clamps, so
a caller error never becomes a different, well-formed frame. Decoders stay
permissive, because disabled factory blocks read back values outside these
ranges (for example a disabled compressor stores ratio 0 and threshold
+340 dB).

| Control | Range enforced by the encoder |
|---|---|
| Volume (input/output), input-source gain | -80 to +12 dB |
| Input-source frequency | at most 20000 Hz (the app offers 10-20000; 0 is allowed, see "Input source") |
| Delay | 0-1200 ms |
| Crossover / EQ / Dynamic EQ frequency | 10-20000 Hz |
| Crossover slope | 6-48 dB/oct, multiples of 6 |
| EQ / Dynamic EQ gain | -20 to +20 dB |
| EQ / Dynamic EQ Q | raw 0-295 (Q 0.404-28.85) |
| EQ type | 0-5 |
| EQ / Dynamic EQ slope | 0-1 (6 dB, 12 dB) |
| Compressor / Limiter / Dynamic EQ threshold | -60 to +20 dB |
| Expander threshold | -100 to 0 dB |
| Attack (start) time, all dynamics | 0.3-200 ms |
| Release time, all dynamics | 50-5000 ms |
| Ratio: Expander, Dynamic EQ | 1-20 |
| Ratio: Compressor | 1-128 |
| Limiter detection type | 0 (average/RMS), 1 (peak) |
| Mix crosspoint | input 0-3, output 0-7 |
| Input source select | 0-3 |
| FIR low/high frequency | 10-20000 Hz |
| FIR attenuation | 21-120 dB |
| FIR type | 0-4 |
| FIR window type | 0-11 |
| FIR order index | 0-1 (`set_fir`) |
| Frequency-shifter level / direction | 0-5 / 0-1 |
| Program slot | 0-29 |

These are the manufacturer's app's limits, not necessarily the device's.
Before the ceiling was enforced, the device accepted +15 dB of
input-source gain and the measured level tracked it within 0.1 dB, so the
firmware does not stop at +12 dB there. Volume above +12 dB has not been
probed; the client refuses it.

## Control families

### Delay (module_id 3)

`module_id=3`, `data_id=3`, `channel_type` IN or OUT, u16 payload,
piecewise:

```
raw <= 4999:  ms = raw / 100.0        # 0.00-49.99 ms, 0.01 ms steps
raw >= 5000:  ms = raw - 4950         # 50-1200 ms,   1 ms steps
encode (truncating):
  ms < 50:  raw = int(ms * 100)
  ms >= 50: raw = int(ms + 4950)
```

Range 0-1200 ms (raw 0-6150), the same for inputs and outputs.
`codecs.delay_u16` implements this. Exposed as
`Client.set_delay(channel_id, ms, *, output=False)` and CLI
`set-delay ip channel ms [--output]`.

**Confirmed on hardware (`hardware_test/test_30_delay.py`).** The test
sweeps 0, 25, 49.5, 50, 75, 300 and 1000 ms on an input and an output,
straddling the 49/50 ms formula boundary, and measures the time-of-flight
shift by cross-correlation. Every step matched within the 0.5 ms
tolerance in both directions. The worst error was 0.15 ms at 1000 ms,
growing with delay at about 150 ppm: a clock offset between the unit and
the measurement interface (see "Sample rate and clock offset").

One channel's delay does not disturb another's. With one output held at
0.6 ms and a second swept across 0-1200 ms, the two channels' relative
alignment, read from the same simultaneous capture, stayed within
0.02-0.14 ms of the expected value at every step, including the
idle-to-active transition. Measured against a reference that bypasses the
unit, a 1 ms or 1000 ms delay on output 2 or input B leaves output 1's
absolute latency unchanged (see "Latency"). Not covered: more than two
channels delayed at once.

Comparing absolute lag across *separate* captures is not valid: capture
start-up jitter alone is about 16 ms with no writes at all. Compare
channels within one simultaneous capture instead.

### Crossover (module_id 38 high-pass, 39 low-pass)

**Output channels only.** One 5-byte block per filter, `data_id=0`:

| Offset | Field | Encoding |
|---|---|---|
| 0 | frequency | u16, raw = Hz, 10-20000 |
| 2 | type | u8: 0 = Butterworth, 1 = Linkwitz-Riley, 2 = Bessel (same mapping for HP and LP) |
| 3 | slope | u8, `dB_per_oct = (raw + 1) * 6`; raw 0-7 = 6/12/18/24/30/36/42/48 dB/oct |
| 4 | enable | u8, 0/1, not inverted (1 engages the filter, confirmed on hardware) |

All eight slopes are active for all three families. The odd-order
Linkwitz-Riley selections (6, 18, 30, 42 dB/oct) are nonstandard firmware
modes, not invalid values; they measure approximately -6 dB at the
configured frequency. Bessel filters sit at -3 dB at the configured cutoff
for both HP and LP (magnitude-normalised). Bessel low-pass matches a
magnitude-normalised Bessel model within about 0.01-0.09 dB over
500 Hz-4 kHz; Bessel high-pass needs calibrated first- and second-order
sections, which match 23 recorded high-pass responses within 0.018 dB /
0.147°. Section models and measurement ranges are in
[FILTER_MODELS.md](FILTER_MODELS.md).

This block has no allpass mode; allpass is an EQ type (see "EQ bands").

**Confirmed on hardware.** A whole-block 1000 Hz / 24 dB-per-octave
high-pass took a 50 Hz tone from -37.4 to -93.8 dBFS. The same setting
sent as per-field frames (data IDs 38-41) was acknowledged but moved the
tone by only ~0.1 dB. `hardware_test/test_40_xover.py` measured HP
200/800/3000 Hz as 195/718/2714 Hz and LP 300/1500/6000 Hz as
300/1484/5677 Hz, all within 0.16 octave. The block layout is confirmed by
readback.

**No crossover on inputs.** A whole-block write with `channel_type=0`
produced no readback change and no audio effect, and the input settings
block has no crossover. `set_xover` therefore has no `output` parameter.

Exposed as `registry.xover_struct_spec(band)` +
`Client.set_xover(channel_id, band, *, freq_hz, filter_type,
slope_db_per_oct, enabled)` (all four fields required) and CLI
`set-xover ip channel {hp,lp} --freq HZ --type N --slope DB_PER_OCT
{--enable|--disable}`.

### EQ bands (module_id 4 + band)

`module_id = 4 + band_index` (0-based), inputs and outputs. This model has
7 bands per input and 15 per output. One 9-byte block per band,
`data_id=0`:

| Offset | Field | Encoding |
|---|---|---|
| 0 | frequency | u16, raw = Hz, 10-20000 |
| 2 | gain | u16, `raw = int((dB + 20) * 10)`, -20 to +20 dB |
| 4 | Q | u16 bandwidth code (below) |
| 6 | slope | u8, 0 or 1 (below) |
| 7 | type | u8, 0-5 (below) |
| 8 | enable | u8, 0/1, not inverted |

The Q field is a bandwidth in octaves, not raw Q:

```
n_octaves = raw * 0.01 + 0.05
Q = 2^(n/2) / (2^n - 1)
encode: n = 2 * log2((1 + sqrt(1 + 4 Q^2)) / (2 Q));  raw = floor((n - 0.05) * 100 + 0.5)
raw 0-295  (n 0.05-3.0 octaves; Q 28.85 down to 0.404)
```

The block layout is confirmed by readback (for example a gain of 7.5 dB,
raw 275, landed at exactly the predicted offset).
`hardware_test/test_50_eq_band.py` confirms gain and Q: a +12/-12 dB
boost/cut measured within 0.1 dB of the configured gain at the configured
frequency.

**Types.** The manufacturer's app offers exactly six EQ types, in this
order; there is no band-pass or band-stop type. Measured behaviour:

| raw | Type | Measured behaviour |
|---|---|---|
| 0 | Peaking | boost/cut at the centre, ~0° phase there |
| 1 | Low shelf | +12 dB configured: +11.9 dB at 100 Hz, +0.9 dB at 8 kHz |
| 2 | High shelf | +12 dB configured: +0.6 dB at 100 Hz, +11.9 dB at 8 kHz |
| 3 | Allpass, first order | for a 1 kHz setting: -53.13°/-90.01°/-126.93° at 500/1000/2000 Hz, flat within ~0.006 dB; no dependence on Q or gain (0-12 dB, Q 0.5/1/2 identical) |
| 4 | Allpass, second order | flat (+0.02 dB), +179.94° at the centre; higher Q gives a steeper phase transition (Q 0.5/1/2) |
| 5 | Notch | finite-depth cut for negative gain only; zero or positive gain is flat; bandwidth convention switches at -6 dB |

The response models in `response.py` reproduce these; equations, measured
error bounds and regression fixtures are in
[FILTER_MODELS.md](FILTER_MODELS.md). In brief:

- **Peaking:** the device's bandwidth is the RBJ formula after
  `Q_effective = Q_wire / 10**(abs(gain_db)/40)`, verified against five
  hardware configurations within 0.011 dB.
- **Notch:** with `L = 10**(gain_db/20)`, use RBJ
  `Q_effective = Q_wire * sqrt(1/L - 2*L)` for `gain_db <= -6`, and
  `Q_wire * sqrt(L)` (the peaking convention) for `-6 < gain_db < 0`.
  The discontinuity at -6 dB is real hardware behaviour, confirmed by
  probes at -5.9/-6/-6.1 dB and across gain, Q and frequency; 22 recorded
  notch responses match within 0.017 dB / 0.190°.
- **Shelves** ignore the stored Q (tested at Q 0.5/1/2) and use fixed
  first- or second-order shapes selected by slope 0/1. The configured
  frequency is not the half-gain point: a +12 dB shelf measures about
  +9.255 dB there. First-order shelves are non-minimum-phase: exactly 0 dB
  bypasses the filter, but at 1 kHz a low shelf at +0.1 or -0.1 dB already
  rotates phase from 0 to about -89.68°.
- **Allpass:** type 3 is modelled as a phase-prewarped first-order digital
  allpass, exact at the configured centre; type 4 as the RBJ second-order
  allpass.

**Slope.** For shelves, 0 = first order and 1 = second order. For types 0,
3 and 5, slope 0 and 1 give identical responses; type 4 also ignored raw
slopes 2 and 3 when they were tried. Other combinations are unconfirmed.
The client accepts only 0 and 1.

**Allpass for alignment.** For subwoofer time/phase alignment, set type 4
(or the first-order type 3) and `freq_hz` to the alignment frequency. The
manufacturer's app shows allpass bands at 0 dB with gain disabled, but the
device stores whatever gain is sent (readback does not zero it), so
`Client.set_eq_band` sends `gain_db=0.0` for types 3 and 4 when the caller
omits it. Every other type requires `gain_db`.

**Confirmed on hardware (`hardware_test/test_60_allpass.py`).** The test
measures the cross-channel transfer function against the untouched output
in the same capture, and compares centre/2, centre and 2× centre with the
model for types 3 and 4. At centre frequencies of 200, 1000 and 1500 Hz,
every reading for both types was within 0.4° of the model (type 4:
-67.4°/180°/+67.3°; type 3: -53.1°/-90.0°/-126.9°) and within 0.06 dB of
flat. Re-aligning each pure-tone capture by correlation cannot do this: a
tone's phase shift is indistinguishable from a time shift, so such a test
can only read ~0° or ~180°.

Exposed as `registry.eq_band_struct_spec(channel_type, band_index)` +
`Client.set_eq_band(channel_id, band_index, *, freq_hz, gain_db=None, q,
type, slope, enabled, output=False)` (every field required except
`gain_db` for types 3 and 4) and CLI `set-eq-band ip channel band_index
--freq HZ [--gain DB] --q Q --type N --slope N {--enable|--disable}
[--output]`.

#### EQ all-bypass and all-reset (0xAF, 0xB0)

Both use `module_id=4`, `channel_type` IN or OUT, `channel_id` = the
target channel and `data_id=0`. 0xAF takes a 1-byte on/off payload; 0xB0
takes an empty payload. Neither works on this firmware:

- **0xAF never replies and has no effect**
  (`hardware_test/test_70_eq_bypass_polarity.py`). A sweep of both channel
  types, `channel_id` 0-3, `module_id` 0 and 4, and both an empty and a
  1-byte payload got no reply in any case, while 0xB0 in the same session
  acknowledged every time. Sent anyway against a live +12 dB EQ boost, it
  produced no measurable change. It still gets no reply after a clean
  power cycle, so this is a persistent firmware gap, not a wedged block.
- **0xB0 acknowledges but changes nothing**
  (`hardware_test/test_72_eq_all_reset.py`). A +15 dB band boost was
  unchanged afterwards, and a readback diff found zero of the snapshot's
  2464 bytes changed.

`Client.eq_all_bypass` and `eq_all_reset` raise `NotImplementedError`
before transport, and the `eq-bypass`/`eq-reset` CLI commands exit
without constructing a client. No other channel-wide EQ bypass mechanism
is known.

**Working fallback (`hardware_test/test_71_eq_bypass_fallback.py`):** set
each band's `enabled=False` with `set_eq_band`. With five bands boosted at
overlapping frequencies (+12 dB, Q=2 each, +15 to +21 dB combined),
disabling all five returned every measured frequency to within 0.04 dB of
the flat baseline. This is one write per band, not atomic. The test covers
bands 0-4; this model has 7 input and 15 output bands.

### Dynamic EQ (module_id 35 + band)

**Input channels only**, 3 bands, `module_id = 35 + band_index`. The
output settings blocks have no Dynamic EQ. One 18-byte block per band,
`data_id=0`:

| Offset | Field | Encoding |
|---|---|---|
| 0 | frequency | u16, raw = Hz |
| 2 | gain | u16, same as EQ gain: `raw = int((dB + 20) * 10)` |
| 4 | Q | u16, same bandwidth code as EQ |
| 6 | slope | u8: 0 = 6 dB, 1 = 12 dB |
| 7 | type | u8: 0 = peaking, 1 = low shelf, 2 = high shelf, 3 = allpass (first order), 4 = allpass (second order) |
| 8 | mode | u8: 0 = engage above threshold, 1 = engage below threshold |
| 9 | threshold | u16, `raw = int((dB + 60) * 2)` |
| 11 | attack (start) time | u16, piecewise (below) |
| 13 | release time | u16, `raw = int(ms - 50)` |
| 15 | ratio | u16, `raw = int((ratio - 1) * 100)` |
| 17 | enable | u8, 0/1, not inverted |

Attack time:

```
ms < 1.0:  raw = int(7.0 - (1.0 - ms) * 10.0)   # 0.3-0.9 ms, 0.1 ms steps
ms >= 1.0: raw = int(ms + 6.0)                  # 1 ms steps
```

Ranges: threshold -60 to +20 dB, attack 0.3-200 ms, release 50-5000 ms,
ratio 1-20. The type, slope and mode lists are the ones the manufacturer's
app offers, with mode labelled "greater than threshold" / "less than
threshold". Types 3 and 4 have not been tested on Dynamic EQ. The
threshold, time and ratio scales are not individually calibrated on
hardware; the threshold and time encodings are shared with the dynamics
blocks below.

**Confirmed on hardware.** `hardware_test/test_90_dyneq_wire_format.py`
confirms by readback that a fully changed block changes the expected
number of stored bytes. `hardware_test/test_91_dyneq_audible_effect.py`
measured a +15.04 dB change for a +15 dB setting, repeatable across three
enable/disable rounds, and all three bands landed within 0.2 dB of the
configured +15 dB. The front panel's Dynamic EQ enable switch writes the
same stored byte the client controls (readback diff while toggling it on
the front panel).

Before a power cycle, the same block showed no audio effect at all across
wide sweeps (every type and slope, both modes with the threshold really
crossed, thresholds -60 to 0 dB, ratios up to 50, centres 200 Hz-4 kHz,
both gain directions), although readback showed every write stored
correctly. That was a wedged processing block, not a protocol problem; see
"Processing blocks can wedge".

There is no all-bands enable or reset on the wire: each band is a separate
whole-block write to module 35, 36 or 37. The client has no all-bands
helper; to reset a band, write every field with known-good values.

Exposed as `registry.dyneq_struct_spec(band_index)` +
`Client.set_dynamic_eq_band(channel_id, band_index, *, freq_hz, gain_db,
qvalue, slope, type, startup_mode, threshold_db, start_time_ms,
release_time_ms, ratio, enabled)` (every field required, no `output`
parameter) and CLI `set-dynamic-eq-band ip channel band_index --freq HZ
--gain DB --q Q --type N --slope N --startup-mode N --start-time MS
--release-time MS --ratio N --threshold DB {--enable|--disable}`.

### Compressor, Limiter, Expander (module_id 40, 41, 42)

All three use the same 10-byte block, `data_id=0`:

| Offset | Field | Encoding |
|---|---|---|
| 0 | threshold | u16 (per block, below) |
| 2 | attack (start) time | u16, Dynamic EQ attack formula |
| 4 | release time | u16, `raw = int(ms - 50)` |
| 6 | ratio | u16 (per block, below) |
| 8 | enable | u8, 0/1, not inverted |
| 9 | detection type | u8 (limiter only) |

Per block:

```
Compressor (module_id 40, outputs; input writes refused):
  threshold:      raw = int((dB + 60) * 2)          -60 to +20 dB
  ratio:          raw = int(ratio * 10)  ("N:1")     1-128
  detection type: unused, sent as 0

Limiter (module_id 41, outputs only):
  threshold:      raw = int((dB + 60) * 2)          -60 to +20 dB
  ratio:          unused, sent as 0
  detection type: 0 = average-sensitive (labelled RMS), 1 = peak-sensitive

Expander (module_id 42, inputs only):
  threshold:      raw = int((dB + 100) * 2)         -100 to 0 dB
  ratio:          raw = int((ratio - 1) * 100)       1-20 (Dynamic EQ's formula, not the compressor's)
  detection type: unused, sent as 0
```

All three: attack 0.3-200 ms, release 50-5000 ms. Unused fields are sent
as 0 (the limiter has no ratio control; the compressor and expander have
no detection type).

Topology matches the settings layout: the output block has a compressor
and a limiter, the input block has an expander and neither of the others.
A compressor write addressed to an input (`channel_type=0`) never reaches
storage on this device (a zero-byte readback delta in
`test_a0_dynamics_wire_format.py`), so the client raises for
`set_compressor(..., output=False)` without sending. No bulk all-enable or
all-reset is known for these blocks.

The compressor's ratio formula has no offset, so it would encode 0.0. The
factory readback of a disabled output compressor does store ratio 0.0 and
a +340 dB threshold. Those values decode, but the encoder rejects them, so
set valid values before enabling the block.

Limiter detection type was identified with repeated equal-peak sine and
17.23 dB crest-factor burst captures: 0 responds to average level, 1 to
peaks. `limiter_struct` rejects any other value.

**Confirmed on hardware.**
`hardware_test/test_a0_dynamics_wire_format.py` (readback diffs): output
compressor 5 bytes changed, limiter 5 bytes, expander 6 bytes (one extra
because the expander's ratio raw value crosses a byte boundary at the
chosen ratio), each exactly what the field formulas predict.
`hardware_test/test_a1_dynamics_audible_effect.py` (tone above/below an
aggressive threshold against a disabled baseline): output compressor
-27 to -28 dB (threshold -40 dB, ratio 20), limiter -28 dB, expander
-79 dB (20:1 well below threshold pushes the signal to the noise floor).

**Expander detector and gain timing** (summary in
`tests/fixtures/expander_hold_measurements.json`). The roughly 1.8 s
closing hold is neither a fixed hold timer nor the configured release.
With attack 0.3 ms, release 50 ms, ratio 10 and a 2 s high segment,
closure began after 1.10, 1.94 and 2.79 s for signals 10, 15 and 20 dB
above threshold. Creating the 10 and 20 dB excesses two ways (moving the
threshold or the signal) agreed within 27 ms. A linear fit gives 168.75 ms
of extra hold per dB (maximum residual 15.02 ms); read as exponential
level decay, that is a detector time constant of about 1.47 s.

Changing the below-threshold tail by 10 dB moved closure onset by only
24 ms, while changing the preceding excess moved it by up to 1.7 s. A
250 ms high segment never opened the expander; segments of 0.5 to 4 s did,
and their closure onset converged from 1.88 to 1.94 s. So the detector
needs finite evidence to open, then decays slowly according to how far
the signal was above threshold. Once the detector crossed the closing
threshold, the 50 ms release case took about 377 ms to reach 63% of final
attenuation, and 50/200/1000 ms releases gave progressively longer
closure ramps: `release_time` controls the gain ramp, not the detector's
decay.

Exposed as:

- `registry.compressor_struct_spec(channel_type)` +
  `Client.set_compressor(channel_id, *, threshold_db, start_time_ms,
  release_time_ms, ratio, enabled, output=False)` / CLI `set-compressor ip
  channel --start-time MS --release-time MS --ratio N --threshold DB
  (--enable|--disable) [--output]`;
- `registry.limiter_struct_spec()` + `Client.set_limiter(channel_id, *,
  threshold_db, start_time_ms, release_time_ms, enabled,
  detection_type=0)` (always outputs) / CLI `set-limiter ip channel
  --start-time MS --release-time MS --threshold DB [--detection-type N]
  (--enable|--disable)`;
- `registry.expander_struct_spec()` + `Client.set_expander(channel_id, *,
  threshold_db, start_time_ms, release_time_ms, ratio, enabled)` (always
  inputs) / CLI `set-expander ip channel --start-time MS --release-time MS
  --ratio N --threshold DB (--enable|--disable)`.

All fields are required on every call.

### Mix matrix (channel_type 2)

The mix matrix addresses a **crosspoint** (one input paired with one
output), entirely inside the payload rather than through `channel_id`:

- `channel_type = 2` (`ChannelType.MIX`);
- `module_id = 0`, `data_id = 0`;
- `channel_id` does not address anything here; the client always sends 0;
- payload, 3 bytes:

  ```
  in_index:  u8, 0-based (0-3)
  out_index: u8, 0-based (0-7)
  value:     u8, 1 = connected, 0 = not connected
  ```

On/off is the only mix control. No continuous mix-level write is known
(module 51 and data ID 82 have never been exercised), and the
manufacturer's app offers only on/off crosspoints.

In the settings snapshot the mixer table occupies bytes 2068-2163 as
32 three-byte entries (4 inputs × 8 outputs). Entries are stored as
(output, input, value), the reverse of the write payload's order; see
[SETTINGS_READBACK.md](SETTINGS_READBACK.md).

**Confirmed by readback:** the table layout matched the unit's untouched
factory-default routes (input C to outputs 5 and 6, input D to outputs 7
and 8). **Confirmed on hardware**
(`hardware_test/test_80_mix_route_polarity.py`): routing input A to
output 2, with input B muted so the crosspoint was output 2's only
possible source, took a 1 kHz tone from the noise floor (~-109 dBFS) to
~-38 dBFS exactly when `set_mix_route(..., True)` was sent, and back when
set to `False`. So 1 means connected.

The 4 × 8 bus count is confirmed (32 snapshot entries, and channel copy
works with 4 inputs and 8 outputs); not every crosspoint has been
individually exercised. The client validates `in_index` 0-3 and
`out_index` 0-7.

Exposed as `registry.MIX_ROUTE` + `Client.set_mix_route(in_index,
out_index, enabled)`, `read_mix_matrix()` (returns
`{in_index: {out_index: connected}}`) and `write_mix_matrix(routes)`, and
CLI `set-mix-route ip in_index out_index on|off`. `write_mix_matrix`
validates every entry first, reads the current matrix, and sends only the
crosspoints that differ, because bursts of rapid writes have preceded
device misbehaviour (see "Known device behaviour").

### Input source (module_id 43)

**Input channels only.** One 6-byte block, `data_id=0`:

| Offset | Field | Encoding |
|---|---|---|
| 0 | source select | u8, 0-3 (below) |
| 1 | frequency | u16, raw = Hz; only used by the sine generator |
| 3 | gain | u16, `raw = int((dB + 80) * 10)` (Volume's formula), -80 to +12 dB |
| 5 | enable | u8, 0/1, not inverted |

Source select, confirmed on hardware with silent playback and generator
gain -40 dB, so any measured signal had to be generated internally:

| Value | Source | Measurement |
|---|---|---|
| 0 | external analog input | silence with silent playback; passes the external signal otherwise |
| 1 | internal sine generator | `freq_hz` sets the pitch: 999.02 / 2000.98 Hz peaks for 1000 / 2000 Hz |
| 2 | internal pink noise | PSD slope -3.09 dB/octave |
| 3 | internal white noise | PSD slope -0.18 dB/octave |

Sources 1-3 replace the external programme material with the internal
signal. On source 0, enable and gain have no audible effect (external
audio passes through this block transparently). On the internal sources,
`enable=False` drops the level by about 35-45 dB and gain tracks the
configured value (`test_a2_input_source.py`); a +15 dB step, above the
app's +12 dB ceiling, measured within 0.1 dB.
With `freq_hz=0` the sine generator is silent (about -108 dBFS), and the
frequency field has no effect on the noise sources.

The manufacturer's app offers 10-20000 Hz for the frequency. The encoder
enforces only the 20000 Hz ceiling, because the factory readback of the
enabled external-source block stores 0 Hz, and requiring 10 Hz would
block every edit of that block.

**Generator characterization** (summary in
`tests/fixtures/secondary_detail_measurements.json`). The sine generator
tracked 20, 100, 1000, 10000 and 20000 Hz. From 1 kHz upward its measured
frequency was 136.8-139.1 ppm low, consistent with the clock offset
measured from delay. Gain settings from -50 to -20 dB produced their
requested 10 dB steps within 0.06 dB; -60 dB fell below the roughly
-109 dBFS measurement noise floor.

The generator is not a laboratory-quality sine source. At 1 kHz / -20 dB
its odd harmonics measured -28.32, -41.46, -50.21, -56.96 and -62.40 dBc
for the 3rd through 11th harmonics, while the simultaneous external
reference measured its own 3rd harmonic at -91.1 dBc. The same odd-harmonic
sequence appears as frequency-shifter sidebands (below), which suggests the
two blocks share one coarse internal oscillator waveform. That shared
implementation is an inference; the spectra are direct measurements.

Exposed as `registry.INPUT_SOURCE_IN` + `Client.set_input_source(
channel_id, *, source_select, freq_hz, gain_db, enabled)` (whole block,
every field required) and CLI `set-input-source`.

### FIR (module_id 49)

Inputs and outputs. One 9-byte parameter block, `data_id=0`:

| Offset | Field | Encoding |
|---|---|---|
| 0 | enable | u8, 0/1, not inverted |
| 1 | filter type | u8: 0 = low-pass, 1 = high-pass, 2 = band-pass, 3 = band-stop, 4 = all-pass |
| 2 | window | u8, 0-11 (see "FIR enumerations") |
| 3 | low frequency | u16, raw = Hz, 10-20000 |
| 5 | high frequency | u16, raw = Hz, 10-20000 |
| 7 | attenuation | u8, `raw = dB - 21`, 21-120 dB |
| 8 | order | u8 index: 0 = 256 taps, 1 = 512 taps |

The per-channel coefficient block (0xC8/0xC9) is separate from these
parameters; see "Whole-device operations". The manufacturer's app's
default FIR settings are window Kaiser and order 512.

**Confirmed on hardware:**

- **Enable and type** (`hardware_test/test_a3_fir.py`): `enabled` toggles
  real filtering, and types 0 and 1 give correctly directed low-pass and
  high-pass shapes. Which frequency field is the corner had to be
  measured: for low-pass the **low** frequency is the corner and the high
  frequency has no effect; for high-pass the **high** frequency is the
  corner and the low frequency has no effect. Types 2-4 are untested.
- **Window** (`hardware_test/test_a7_fir_win_type.py`): next to the
  transition (2100-2300 Hz, just above a 2000 Hz corner), window 5
  measured about 9-10 dB *less* attenuated than windows 1 and 2 (for
  example -36.3 dB against -44.8/-46.1 dB), reproducibly. That matches
  window theory: Blackman-Harris trades a wider main lobe for a deeper
  sidelobe floor than Hanning/Hamming. In the deep stopband (6-9 kHz)
  windows 1, 2 and 5 all reached the same -89 to -91 dB, which is the
  measurement floor, not a window effect. Window 0 at attenuation 21 dB
  versus 80 dB moved the far-stopband floor by 17.5-17.6 dB in two runs,
  while window 2 moved by 0.1 and -3.8 dB: a parameterised window
  responding to its shape parameter next to a fixed one. The 21 dB
  attenuation floor is also the break point of the standard Kaiser design
  formula (attenuation at or below 21 dB gives beta = 0). Together these
  support 0 = Kaiser. Windows 3, 4 and 6-11 are untested, and the full
  list mapping is not confirmed.
- **Order** (`hardware_test/test_a8_fir_order.py`): order indices 0-3 all
  produced a working low-pass (48-54 dB of stopband attenuation, corner
  within tolerance of 2000 Hz). Index 0 measured an approximately 2× wider
  transition (0.21 octave between the -3 dB and -30 dB points, against
  0.10) and a ~6 dB shallower stopband (48 dB against ~54 dB) than
  index 1, reproducibly across three runs. Indices 1, 2 and 3 measured
  identically. Half the taps doubling the transition width fits 0 = 256
  and 1 = 512 taps, and the impulse response confirms the counts
  (`hardware_test/test_a9_base_latency.py`): with a rectangular window
  (Kaiser at 21 dB), the FIR stage's response spans 256 and 512 taps at
  96 kHz, within the 48 kHz capture's two-tap resolution (measured 257 and
  513). Indices 2 and 3 alias to 1 on this firmware; the
  manufacturer's app offers only 256 and 512 for this model, so
  `set_fir` accepts only 0 and 1.
- **Latency** (`hardware_test/test_a9_base_latency.py`): an enabled FIR is
  linear phase and adds 2.662 ms (256 taps) or 3.995 ms (512 taps). A
  Kaiser-window write at 21 dB attenuation on any channel also adds 0.667
  ms to every path; see "Latency".

Exposed as `registry.FIR_IN`/`FIR_OUT` + `Client.set_fir(channel_id, *,
enabled, fir_type, win_type, freq_low_hz, freq_high_hz, attenuation_db,
order_index, output=False)` (whole block, every field required) and CLI
`set-fir`.

### Frequency shifter (module_id 48)

**Input channels only.** One 3-byte block, `data_id=0`:

| Offset | Field | Encoding |
|---|---|---|
| 0 | level | u8, 0-5 |
| 1 | direction | u8: 0 = down, 1 = up |
| 2 | enable | u8, 0/1, not inverted |

**Confirmed on hardware (`hardware_test/test_a4_freq_shifter.py`).** On a
steady 1 kHz tone, direction selects the sign of the shift and the six
levels give approximately 1, 2.5, 3.5, 5, 7 and 9 Hz. Upward shift
(direction 1) works on the device.

**Carrier independence and spectral quality** (summary in
`tests/fixtures/secondary_detail_measurements.json`): levels 0 and 5
measured ±0.99844 Hz and ±8.99676 Hz at carriers of 100 Hz, 1 kHz and
10 kHz, with a spread below 0.00001 Hz across carriers. This is a
fixed-hertz frequency shifter, not a pitch shifter.

The principal shifted tone comes with a stable odd-sideband series. For a
level-5 shift, the opposite-direction 3× sideband was -28.65 dBc, the
desired-direction 5× sideband -42.01 dBc, then -50.92, -57.48 and
-62.85 dBc at alternating 7×/9×/11× offsets. These closely match the
internal sine generator's odd harmonics. Carrier leakage and the ordinary
opposite image were much lower. Use the block for small decorrelation
shifts, not as a clean single-sideband translator.

Exposed as `registry.FREQ_SHIFTER_IN` + `Client.set_freq_shifter(
channel_id, *, freq_level, direction, enabled)` (whole block, every field
required) and CLI `set-freq-shifter`.

## Whole-device operations

| Opcode | Operation | Header | Payload | Client default timeout |
|---|---|---|---|---|
| 0xAB | save live state to a program slot | `user_id` = slot | empty | 5 s |
| 0xAC | load a program slot | `user_id` = slot | empty | 9 s |
| 0xC8 | read one channel's FIR coefficients | `channel_type` 0/1, `channel_id`, `user_id=0` | empty | 1.8 s |
| 0xC9 | write one channel's FIR coefficients | `channel_type` 0/1, `channel_id`, `user_id=0` | 2048 bytes | 1.8 s |
| 0xB5 | copy one channel's settings to another | `channel_type` 0/1, `channel_id` = **source** | ASCII bitmap, one character per channel: `'T'` (0x54) = target, `'N'` (0x4E) = not | 1 s |

`module_id` and `data_id` are 0 for all of these.

### Program save and load

`save_program(slot)` / `load_program(slot)`, CLI `save-program` /
`load-program`. `slot` is the 0-based program index (30 slots, 0-29; their
names come from 0xCA). `load_program` **overwrites every channel's live
settings at once**, the largest blast radius of any implemented operation.
It does not re-read FIR coefficients afterwards; call `read_fir_coefs` per
channel if you need them.

**Confirmed on hardware (`hardware_test/test_a6_program_slots.py`, slot 0,
empty beforehand):** a saved EQ boost (+15 dB at 1 kHz, measured
-23.07 dBFS) was changed to -12 dB (-49.99 dBFS) and then loaded back,
measuring -22.97 dBFS, within 0.1 dB of the saved state.

### FIR coefficient backup and restore

`read_fir_coefs(channel_id, *, output=False)` /
`write_fir_coefs(channel_id, coefficients, *, output=False)`, CLI
`read-fir-coefs` / `write-fir-coefs`. Each channel's coefficient block is
512 little-endian float32 values (2048 bytes). The API keeps the block as
opaque bytes and assigns no tap semantics. Enabling a parameter-designed FIR
leaves the block unchanged: on input A and output 1, with the FIR enabled at
either order, it read back as the same unit impulse
(`hardware_test/test_a9_base_latency.py`), so the designed coefficients are
not readable through 0xC8. Restore rejects any payload that
is not exactly 2048 bytes or contains a non-finite float32, sends one
complete block with `user_id=0` and the channel address, and the CLI
command immediately reads the block back and checks it exactly.

**Confirmed by readback.** An unchanged output-2 block first completed an
idempotent write/read cycle. Separate output-2 and input-B trials then
changed only coefficient 511 from zero to `0.0625`, read back exactly the
changed 2048 bytes through 0xC8, and restored the original block (a unit
impulse) exactly. After every trial the 2464-byte settings snapshot hash
was unchanged
(`01c5d961c1f32dec4bc7f2f3e710ca63e50f11388a0963e3c1c71f84396f49dc`), all
twelve FIR coefficient hashes matched their baseline, and control and
audio checks passed.

### Channel copy

`channel_copy(source, targets, total_channels, *, output=False)`, CLI
`channel-copy`. The header `channel_id` is the **source**; the target is
marked in the payload bitmap (`codecs.channel_copy_bitmap`).
`total_channels` must be 4 for inputs or 8 for outputs; other bitmap
lengths are rejected before transmission. The manufacturer's app copies to
one target at a time, and `channel_copy` likewise requires exactly one
target.

**Confirmed on hardware (`hardware_test/test_a5_channel_copy.py`), both
directions:** with the source channel's EQ boosted and the target flat,
after the copy the target's measured output moved to within 0.1-0.6 dB of
the source's.

### Settings snapshot (0xA9)

`read_group()` returns the raw 2464-byte snapshot and `read_settings()`
decodes it; the packed layout and decoder are documented in
[SETTINGS_READBACK.md](SETTINGS_READBACK.md). The snapshot does not
include FIR coefficients (read those with 0xC8).

`blobdiff.diff_blobs(before, after)` and the `diff-group` CLI command
locate a control's bytes empirically: dump the snapshot, make one known
change, dump again, diff. `diff-group` uses diff(1) exit codes: 0
identical, 1 different (a length mismatch counts as different), 2 for an
unreadable file. `dump-group` only writes a reply that decodes as a valid
snapshot (`--no-validate` to override), and writes atomically so a failure
never truncates an earlier backup.

## Not implemented

### WriteGroup (0xA8)

0xA8 writes a whole settings snapshot. As far as is known it is one
single frame carrying the entire snapshot, not chunked, with `user_id`,
`version` and `device_number` taken from the session (not confirmed on
hardware). The client deliberately does not send it:

- It has a much broader blast radius than any other write: every
  channel's settings at once.
- The live read is 2464 bytes, 2164 packed settings bytes plus 300 bytes
  of channel names; how a write handles those trailing name bytes, and any
  readback-only fields, is unresolved.
- The snapshot does not carry FIR coefficients. They are restored
  separately, per channel, with 0xC9, which is now implemented and
  confirmed, so FIR restoration does not depend on 0xA8.

Keep 0xA8 unavailable until a typed, exact-restoring trial is designed.

### Other gaps

- **Factory reset (0xB1):** deliberately not automated.
- **Firmware update (0xAD), passwords (0xB8, 0xD8):** out of scope on a
  trusted network.
- **0xC5:** payload not decoded.
- **Program rename (0xCB):** refused; see "SetUserName (0xCB)".
- **Modules without a client control** (reverb 44, automatic gain control
  45, noise gate 46, feedback suppressor 47 and 52, module enable 50,
  mixer level 51, system 53) have never been exercised. No working reverb
  write is known.
- **Solo:** the manufacturer's app has no solo control, and none is known
  on the wire.
- **Dynamic EQ all-bands enable/reset:** no wire operation exists; write
  each band instead (see "Dynamic EQ").
- **USB:** the unit also has a USB port. USB control is not implemented;
  the network transport is what's in use.

## CH9120 discovery/config protocol (`discovery.py`)

The unit's network interface is a WCH CH9120 serial-to-Ethernet bridge
chip. Its configuration protocol uses a fixed 285-byte packet, broadcast
over UDP to port 8087:

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 16 | flag | ASCII `CH9120_CFG_FLAG` plus one NUL |
| 16 | 1 | cmd | 1 = set, 2 = get, 3 = reset, 4 = search; replies are expected to set bit 0x80 (never observed) |
| 17 | 6 | id | device MAC, filled in by the device in replies |
| 23 | 6 | cfg_mac | |
| 29 | 1 | len | bytes of `data` used (at most 255) |
| 30 | 255 | data | |

`cmd` and `len` are single bytes. A 291-byte variant with 4-byte `cmd` and
`len` fields also got no reply.

**Status: unsupported on this bridge/firmware.** No genuine reply has ever
been observed. The search packet was sent unicast to the device and as
both limited and subnet broadcasts, with the local socket bound to port
8087, with default routing and with an explicit source address, while the
application protocol worked normally over the same port from the same
host. One apparent "reply" turned out to be the host's own broadcast
looped back by the local network stack.

`search()` and the CLI `search` command are kept as a best-effort
implementation and accept an optional source address (`--source-ip`) for
multi-interface hosts, but expect an empty result. The unit's front panel
shows its IP address; **entering it manually
(`sinbosen-la548 status <ip>`) is the reliable path.**

## Known device behaviour and open questions

### Processing blocks can wedge until a power cycle

A processing block can stop affecting the audio while still acknowledging
writes and storing them correctly (readback shows the new values). This
was observed for Dynamic EQ after a session that included raw sends of
non-standard 0xAF payloads; the exact trigger was not isolated. A power
cycle fixed it immediately: the same test that measured no effect before
measured a clean +15.04 dB afterwards. The DSP appears sensitive to
malformed or off-protocol packets.

So when a write confirmed by readback shows no audio effect, and
especially if any raw or malformed frames were sent earlier, power-cycle
the unit and re-test before concluding the control doesn't work. This
failure looks identical to a wrong wire format from the outside. (0xAF
still got no reply after the same power cycle, so its failure is a
different, persistent one.)

### An output can go silent until a power cycle

Once, output 2 read as pure noise floor (about -91 dBFS) across repeated
measurements while output 1 measured cleanly on the same signal, with
routing, mute, volume, polarity and delay all confirmed correct by
readback. A plain power cycle fixed it with no wiring or configuration
change; the cause was not identified. A burst of rapid automated writes
preceded it. If a channel reads steady silence despite correct state,
power-cycle the unit before suspecting a jack, cable or converter.

### Network outages

The unit became unreachable on the network (no ping replies) twice during
long test sessions. Once was
right after rapid automated connect/disconnect cycling (several `Client`
sessions opened and torn down within a couple of seconds), a traffic
pattern worth avoiding. The other came after about 20 minutes with no
traffic at all, so client traffic did not cause it. Both recovered with no
lasting configuration change. The 0xD9 trial is not related: the device
stayed reachable throughout it.

### Latency

**Confirmed on hardware (`hardware_test/test_a9_base_latency.py`).**

**Base latency.** Analog input A to analog output 1 takes **1.732 ms**
(166.3 samples at 96 kHz) with the path's processing flat (no EQ,
crossover, dynamics or FIR enabled) and every delay at 0 ms. Three captures
agreed within 0.1 µs, and the cross-correlation peak and the 1-4 kHz group
delay agreed within one capture sample.

**FIR writes can add 64 samples to every path.** After a FIR parameter
write that uses the Kaiser window (window 0) at 21 dB attenuation, the
bottom of its range, every path is 64 samples (0.667 ms) slower: input A to
output 1 takes **2.399 ms**. A Kaiser write with any higher attenuation (22
dB is enough) removes the extra delay, and writes with other windows leave
it as it is. The state follows the most recent such write on any channel,
with the FIR enabled or not, including channels outside the measured path
(output 8, input D). A volume write does not change it. 21 dB is where the
standard Kaiser formula gives beta = 0, a rectangular window. Every FIR
block on the unit measured was stored at Kaiser 21 dB; the state at power-on
was not tested. To get the shorter latency, make the last Kaiser-window FIR
write use more than 21 dB, on any channel and with the FIR enabled or not.

**Enabled FIR.** An enabled FIR adds (N - 1) / 2 + 128 samples at 96 kHz to
its channel: **2.662 ms** for 256 taps (order index 0) and **3.995 ms** for
512 taps (index 1). The (N - 1) / 2 part is a symmetric FIR's group delay;
the 128 samples are a fixed extra. Input A and output 1 measured the same.
With a 20 kHz low-pass, the added delay was the same from 200 Hz to 10 kHz
within 1 µs, so the FIR is linear phase.

**Delay elsewhere.** A 1 ms or 1000 ms delay on output 2 or on input B moved
output 1's latency by less than 0.1 µs.

**Polarity.** Against the reference, output 1 is inverted with both
polarity fields at raw 0, matching "Polarity".

**Method.** The interface output that drives input A was Y-split, with the
second leg returning straight to the interface, and the latency is the
LA548 path's lag minus that reference's lag from the same capture. The
interface's own latency and the capture start-up jitter cancel. Absolute
accuracy is a few microseconds, limited by the reference leg's transformer
isolator and by the two interface inputs used; differences between
settings, such as the FIR and Kaiser figures above, are free of both.
Without the isolator, connecting the reference leg to the interface input
made LA548 input A stop passing signal entirely.

Not measured: other input/output pairs, FIR enabled on both the input and
the output of one path, and the delay that IIR processing adds. IIR EQ and
crossovers add frequency-dependent group delay (see
[FILTER_MODELS.md](FILTER_MODELS.md)).

### Sample rate and clock offset

No opcode reports the DSP's sample rate. The response models
(`response.py`, [FILTER_MODELS.md](FILTER_MODELS.md)) and the settings
profile use **96 kHz**. Two independent measurements below confirm it on
hardware, so the profile marks it verified (`sample_rate_verified=True`).

The first is how well measured responses fit. The peaking, shelf, allpass
and notch models are bilinear-transform designs with no fitted constants,
so their shape depends on the assumed rate. Against the 55 recorded EQ
responses in `tests/fixtures/` (the shelf, input-EQ and notch captures,
50 Hz-15 kHz), they match within 0.022 dB / 0.19° at 96 kHz, but only
within 0.25 dB / 8.2° at 48 kHz, 0.022 dB / 0.45° at 88.2 kHz and
0.067 dB / 1.9° at 192 kHz. 96 kHz is therefore the best-supported
nominal rate on response shape alone, though 88.2 kHz fits nearly as
well. (The Bessel high-pass section table was fitted at 96 kHz, so it is
not independent evidence.)

The second, FIR timing, is independent of those fits. A symmetric FIR of
N taps delays by (N - 1) / 2 samples, a half-integer for even N, and an
enabled FIR adds that plus a whole 128 samples (see "Latency"). Allowing
for the unit's clock running about 140 ppm slow, the measured 2.6618 ms and
3.9954 ms are 255.50 and 383.50 samples at 96 kHz, but 127.75 and 191.75
samples at 48 kHz. The FIR's impulse response also spans 256 and 512 taps
at 96 kHz, the two lengths the manufacturer's app offers
(`hardware_test/test_a9_base_latency.py`). At 88.2 kHz those spans would
be 235 and 470 taps, and at 192 kHz the fixed part would be 255.5 samples
rather than a whole number. The rate is still not read from the device, so
a firmware running at another rate would go unnoticed.

Separately, the unit's clock runs about 140-150 ppm slow relative to the
measurement interface: the sine generator measured 136.8-139.1 ppm low
from 1 kHz upward, and delay error grows at about 150 ppm. This is a clock
offset between the two devices and says nothing about the nominal rate.

### Input compressor has no effect

On this firmware an input-compressor write never reaches storage (a
zero-byte readback delta) and the input settings block has no compressor.
`set_compressor` raises for inputs without sending. Output compressor,
limiter and expander all work.

### EQ all-bypass and all-reset have no effect

0xAF never replies and 0xB0 acknowledges without changing anything; the
client refuses both. Disable bands individually instead (see "EQ
all-bypass and all-reset (0xAF, 0xB0)").

### Measurement notes

These pitfalls produced false results during hardware testing:

- A pure tone's phase shift is indistinguishable from a time shift.
  Measure phase against an untouched channel in the same capture, not by
  re-aligning each capture by correlation.
- Absolute lag compared across separate captures carries ~16 ms of
  capture start-up jitter; compare channels within one capture.
- A deep-stopband reading at the measurement floor (about -89 to -91 dB
  here) can't distinguish filter shapes; measure near the transition.
- An unrelated write just before a measurement can briefly dip a
  correlation's magnitude (never its sign); allow settling or retry.
- Size capture-to-capture jitter with a disabled-versus-disabled control
  before trusting a small transient or envelope "effect".
- Before accepting a null result, sweep every raw value of a selector
  (input source 0 looked like "enable and gain do nothing") and check
  dependent fields (the sine generator is silent at 0 Hz).

### Open questions

- Linkage (header byte 2): semantics and group mapping.
- Version/machine-model byte: requests use 10 and replies have shown 10
  and 19; other values are unmapped.
- HandShake reply: per-channel meter offsets.
- Settings snapshot: many inner fields remain unresolved (for example two
  base-block bytes); FIR coefficient blocks are treated as opaque.
- EQ slope values other than 0/1 for non-shelf types; Dynamic EQ types 3
  and 4; FIR types 2-4; FIR windows other than 0, 1, 2 and 5.
- Polarity direction (which raw value is 0°).
- Volume above +12 dB.
- Delay: more than two channels delayed at once.
- Latency of input/output pairs other than A to 1, and with FIR enabled on
  both ends of one path; whether the Kaiser 21 dB latency state is set at
  power-on.
- Individual mix crosspoints beyond those exercised.
