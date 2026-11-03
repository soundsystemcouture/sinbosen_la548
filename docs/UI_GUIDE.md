# LA548 DSP Studio

## Install and launch

Requires Python 3.12 or later. From the checkout:

```sh
uv sync --extra ui
uv run --extra ui sinbosen-la548-ui --demo
```

Open <http://127.0.0.1:7860>. `--port` changes the port. The application binds
to `127.0.0.1`, disables public sharing and Gradio analytics, and starts offline.
The optional UI extra adds Gradio, NumPy, SciPy, and Plotly; the core client has
no runtime dependencies. `--demo` documents the default behavior.

## Connect, preview, Apply, Live

Enter the DSP IP address and **Connect**. Connection reads the lock state and
actual snapshot without changing settings. Unsupported or malformed snapshots
are refused. The current layout is DA0408X: 4 inputs, 8 outputs, 7 input EQ
bands, and 15 output EQ bands. This is a validated snapshot layout rather than
automatic firmware identification.

Choose a channel in the sidebar. Edits change a browser-specific draft and its
plots. Channel switching retains drafts. **Pending changes** shows the original
readback and draft values. **Revert draft** restores the last readback. **Refresh**
merges unrelated external changes while retaining your edits; a conflicting
external edit is reported rather than silently overwritten. Revert a conflicting
draft and refresh to load the external version before editing again.

EQ/allpass frequency and Q, crossover frequencies, and channel delay settle
after 250 ms of typing. **Enter** or leaving a field commits it immediately.
Blank or invalid values stay in the field with a red border and an explanation
below it; the response continues to use the last valid setting. Correct the
highlighted value or use **Revert draft**. Invalid numeric edits block Apply and
enabling Live, and never reach the device.

**Apply changes** validates edited controls, checks device locking, reads current
settings for conflicts, writes only changed blocks, then verifies readback. An
ACK alone is insufficient. A partial failure reports verified controls and keeps
unapplied edits pending. A missing verification readback leaves writes unverified.
Retry Apply only after resolving the reported connection or conflict issue.

**Live tuning** requires a clean draft. Apply or revert existing edits first.
Sliders submit on release; typed numeric edits settle after 250 ms without a newer
edit, while Enter or leaving the field commits explicitly. Repeated edits to a
block coalesce. Live is disabled on connection loss,
conflict, write/verification failure, program operations, and preset import.
Reconnect retains pending drafts for the same device without sending them.
Connecting to a different device requires reverting pending edits first.

One server-owned client serializes all device operations, preserving the client's
transport pacing. Connecting from another browser replaces that connection;
earlier sessions retain their drafts but must reconnect before writing. Use one
browser for tuning a device. Browser drafts expire after an hour; export a preset
to keep them. Stop the application with Ctrl-C to close its device transport.

**Offline demo** disconnects and resets that browser's DSP draft to the bundled
hardware fixture. Apply, Live, programs, and copy are simulated locally. Snapshot
download returns the original demo fixture; it is not an encoder for simulated
settings. Separate coefficient data is unavailable in demo mode.

## Crossovers and allpass

Output HP/LP cards are adjacent. Select enable, corner frequency, family, and
slope. All three families allow 6–48 dB/oct. The odd-order Linkwitz–Riley modes
use the LA548's nonstandard measured sections rather than textbook LR filters.

Select an EQ row or band, then edit enable, frequency, gain, Q, and filter type.
**Add allpass** consumes the first disabled EQ slot and initializes a second-order
allpass at 80 Hz, Q approximately 1. The actual Q is quantized through the device
codec. Allpass gain is fixed; frequency and Q control its phase transition and
group-delay peak. A full channel reports that no slot is available.

**Bypass all EQ** stages each band disabled and remembers the original enable
mask. **Restore EQ** restores that mask while retaining parameter edits. Restore
before allocating another allpass. Saved bypass masks are discarded on revert,
connect, demo reset, preset import, and successful program/copy operations.
The firmware's nonfunctional global bypass command is not used.

Sidebar controls manage gain, mute, polarity, and delay. The **Mixer** tab is
binary input-to-output routing; it has no continuous crosspoint gain.

## Response plots and export

Magnitude, phase, and group delay share a logarithmic frequency axis. Hover
shows numerical values; zoom/pan and legend visibility use Plotly controls.
Toggle individual filters, readback overlay, phase unwrap, and subwoofer zoom.
**Export response plots** downloads a ZIP containing standalone interactive HTML,
curve CSV, and the displayed model notes. The Plotly mode bar also exports an image.

The channel plot models that channel's processing. The alignment view composes
the selected input and each routed output. Neither includes loudspeaker, room,
amplifier, or the unit's fixed input-to-output latency (1.732 ms on input A to
output 1, or 2.399 ms after a Kaiser 21 dB FIR write; see
[PROTOCOL.md](PROTOCOL.md) "Latency").

Coefficients are estimates: RBJ peaking (Q-corrected against hardware), measured shelf shapes,
first- and second-order allpass/a negative-gain notch (with its measured
bandwidth switch at -6 dB), digital Butterworth, doubled Butterworth for
standard even-order Linkwitz–Riley, measured firmware-specific odd-order
Linkwitz–Riley sections, magnitude-normalized Bessel low-pass, and Bessel
high-pass sections calibrated against the LA548. See
[FILTER_MODELS.md](FILTER_MODELS.md) for measurement coverage and limits.
The models run at 96 kHz, the DSP rate confirmed on hardware (see
[PROTOCOL.md](PROTOCOL.md) "Sample rate and clock offset"). Delay phase
and pure-delay group delay are calculated analytically; per-section
continuous phase prevents long-delay/cascade unwrap errors.

Shelf Q is stored by the device but has no measured effect. Shelf slope 0
is first order and slope 1 is second order. A first-order shelf at exactly
zero gain is bypassed; even a ±0.1 dB setting introduces substantial phase
rotation. The preview includes this device behavior. Shelf frequency is a
prototype corner rather than the usual half-gain midpoint.

Phase and group delay are masked near nulls (below -120 dB relative to peak or
near numerical zero). Magnitude remains visible. Enabled unknown EQ types,
unresolved EQ slopes, unsupported LR orders, FIR, dynamic EQ, dynamics, shifting,
or unmodeled source selection mark totals incomplete and list omitted stages.
An incomplete curve predicts only the stages explicitly modeled. Exact device
coefficient agreement requires hardware comparison.

## Open Sound Meter traces

Import a single native OSM stored-trace `.osm`, or a `.txt`, `.csv`, or `.frd`
export. Native projects and live streaming are deferred. Text accepts ordered
columns: **frequency in Hz, magnitude in dB, optional phase in degrees, optional
coherence from 0 to 1**. Whitespace, comma, and semicolon delimiters are supported,
as are REW headers/comments and OSM export headers. Frequencies must increase;
DC bins are omitted from logarithmic plots. At least three positive samples are
required. `*`, empty CSV fields, `NaN`, and `null` preserve missing values as gaps.
Magnitude-only files stay magnitude-only and cannot participate in complex sums.

Native OSM uses linear transfer magnitude and phase in radians. The importer
reproduces stored display gain, delay in ms, polarity, inverse magnitude, and
ignore-coherence semantics. Stored impulse data is retained with its gain,
polarity, and time adjustments. These follow OSM's
[Stored implementation](https://github.com/psmokotnin/osm/blob/master/src/source/stored.cpp).
OSM text exports may contain adjusted magnitude but omit stored delay/polarity
from phase; prefer native `.osm` when alignment depends on those adjustments.

Rename, show/hide, associate traces with an output, or set coherence masking.
Imported samples and original file bytes remain immutable. Filters configured in
the DSP are never applied again to imported measurements.

## Alignment

Select a **common input** and two distinct outputs in **Alignment**. The electrical
comparison shows both paths, their complex sum, and B-minus-A unwrapped phase.
An unrouted path has no response. Preview alignment delay/polarity changes with
the dedicated controls, then Apply (or use already-enabled Live). Refresh the
comparison after other tuning changes.

For measured sums, select two phase-bearing traces and confirm that they share
timing and level references. Different known sample rates are refused. The sum
uses overlapping frequency samples, interpolates magnitude in log frequency and
continuous phase in linear frequency, and preserves gaps. Confirming timing does
not align traces automatically. Imported phase resolution limits interpolation
accuracy; independent capture clocks and unknown reference offsets cannot be
repaired by the importer.

## Advanced controls, FIR, presets, and programs

**Advanced** offers only blocks present on the channel: output compressor/limiter,
input expander/dynamic EQ/source/shifter, and FIR parameters on both directions.
Edit the Value column and **Preview block changes**. Boolean values are `true`
or `false`; whole-block codec validation occurs before staging. Disabled hardware
readback may contain out-of-range dynamics values: replace those with valid values
before staging that block. Unused dynamics bytes are preserved by refusing writes
when the current client cannot preserve them.

Block descriptions explain verified enums and remaining uncertainty, including
which FIR frequency field is the low-pass and high-pass corner. On the DA0408X,
order index 0 selects 256 taps and index 1 selects 512 taps for both inputs and
outputs. The management tab can download or restore the selected channel's exact
2048-byte coefficient block. Restore validates all 512 float32 values locally,
requires a clean unlocked device, verifies exact coefficient readback and an
unchanged settings snapshot, and attempts rollback after a readable mismatch.

FIR type 5 runs the channel's coefficient block instead of a designed filter, but
only once a block has been written after selecting it: apply type 5, then restore
a coefficient block to that channel. It reads back as type 6, which can be written
back unchanged. Plots do not include an enabled FIR. The designed types are
linear phase and delay their channel by 2.662 ms (256 taps) or 3.995 ms (512
taps); type 5 delays by 1.333 ms plus the block's own delay. The alignment view
does not include either delay.

Local preset JSON is versioned and profile-specific. Export captures the draft's
supported controls; import stages changes with Live off. Presets omit names,
unknown fields, program contents, and separate FIR coefficient payloads. Import
refuses incompatible profiles, structure, and malformed values.

The DA0408X exposes 30 program slots with zero-based wire indices 0–29. Save records
current device settings, while load replaces all channels and routes. Applied changes
live only in the unit's working settings and are lost at power-off unless saved to a
slot. The unit powers on into its current program, shown on its front panel as
"Scene: UserN"; saving or loading a slot makes that slot current. Channel copy
is within the selected input/output direction. Review
destinations with **Review destinations** and confirm before execution; pending drafts must be applied or
reverted. Each operation disables Live and reloads actual settings, including
after a missing ACK where readback is still possible. If readback fails the service
disconnects and requires reconnect. Program/copy actions do not read or decode
separate FIR coefficient buffers automatically.

## Verification and remaining acceptance

The unit suite covers packed snapshot boundaries, codecs, response mathematics,
OSM semantics, service writes/conflicts/partial failure, Live, presets, operation
destinations, and offline Gradio callbacks. Chromium exercises the real assembled
application through an injected fake client (see "Browser E2E and review videos"
below).

## Dragging EQ frequencies and seeing unapplied edits

The magnitude plot has circular handles for enabled EQ/shelf bands and diamond
handles for allpass. The selected disabled band has a hollow handle; moving it
does not enable the band. Click a handle to select its editor below, or choose
the band from the dropdown when handles overlap. Handle height is the band's
configured gain, rather than the combined response. Allpass handles stay at 0 dB.

Drag horizontally to change frequency. The magnitude, phase and group-delay
curves preview the movement. Frequency stops at the visible zoom bounds and
the device's 10–20,000 Hz limits. Gain and Q are still edited below, and the
frequency box remains available. Crossovers and measurements have no handles.

Starting a drag switches **Live tuning** off. Releasing stages the frequency
locally; use **Apply changes** to write and verify it. Live stays off until you
enable it again after Apply or Revert. Escape or pointer cancellation discards
the movement. Changing channel/band, refreshing, or changing the draft ends
the drag. Dragging replaces unfinished frequency input for that band; other
highlighted input errors still require correction.

The persistent **Unapplied changes** banner counts controls across every
channel, including drafts outside the current view. The Pending changes table
shows their before/draft values. Returning a control to its readback value
removes it from the count. Failed or partially completed Apply keeps remaining
edits visible, including after disconnect. Unfinished numeric input has its
own warning; offline demo changes are explicitly marked as local.

## Browser E2E and review videos

The browser suite starts a separate localhost Gradio server for every scenario.
It accepts only the fake hostname `e2e.fake.invalid`, records its fake-device
operation ledger, and never creates a production device client. Ordinary test
runs skip this suite unless `--run-e2e` is present.

```sh
uv sync --extra ui --group e2e
uv run --group e2e playwright install chromium
uv run --extra ui --group e2e pytest tests/e2e --run-e2e --review-videos
```

Each run creates a new `ignore/playwright/<timestamp>-sinbosen_la548/` directory.
Its `index.html` links each 1920×1080 silent WebM recording, Playwright trace,
final screenshot, client diagnostics, and inspected downloads. `--review-videos` pauses and scrolls
completed crossover/allpass, mixer, alignment, FIR, and management states; the
rapid Live numeric burst remains unpaced so it still tests coalescing. Browser
page errors and console errors fail the test and leave diagnostics in that run.
The scenarios include recorded drag, cancellation, editor synchronization and
global unapplied-status coverage.

`python scripts/check_ui_browser.py --review-videos` is a convenience wrapper
around the same scenarios. Pass `--artifacts /some/local/path` to change their
parent directory; no artifact is uploaded or overwritten.

UI hardware acceptance remains opt-in: compare readback to the manufacturer's
app, make reversible mixer/crossover/allpass changes through a hardware loopback,
and check reconnect without replay. Compare representative imported native/text
traces visually against OSM. Existing client HIL findings inform the UI but do
not establish that the UI has completed hardware acceptance.
