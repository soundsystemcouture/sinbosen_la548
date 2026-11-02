# Measured LA548 filter models

The notch and Bessel high-pass previews use models checked against recorded
audio rather than textbook approximations. Bessel high-pass uses an empirical
section table, not a claim that the firmware implements the ideal Bessel
polynomial. A separate shelf measurement set established its slope selection,
frequency convention, and non-minimum-phase behavior.

Twelve input-B band-0 measurements independently confirmed the same models
for types 0–5 at selected slope 0/1 settings, including the
-5.9/-6 dB notch transition. Across 59 tones from 50–15000 Hz, maximum error
was 0.00981 dB / 0.07023 degrees. These captures are retained in
`tests/fixtures/input_eq_measurements.json` and exercised by the measured
response regression test. The full settings snapshot was restored exactly
afterwards; this confirms selected input/output parity, not all bands or ranges.

## Notch: a bandwidth switch at -6 dB

Let `g` be the configured gain in dB, `Q` the decoded wire Q, and
`L = 10**(g/20)` the linear center amplitude. In the standard RBJ peaking
topology, use:

| Gain | Effective RBJ Q |
| --- | --- |
| `g >= 0` | Unity response; Q is irrelevant |
| `-6 < g < 0` | `Q * sqrt(L)` (same coefficients as LA548 peaking EQ) |
| `g <= -6` | `Q * sqrt(1/L - 2*L)` |

For deep cuts, the normalized analog form is
`H(s) = (s² + D*L*s + 1) / (s² + D*s + 1)`, where
`D = 1 / (Q * sqrt(1 - 2*L²))`. This defines the bandwidth at half power
(approximately -3 dB). Digital prewarping places the center at the requested
frequency. The shallow-cut branch uses `D = 1/(Q*L)` instead.

The change at -6 dB is discontinuous on the actual device. At fc=1 kHz,
requested Q=1, the measured response at 500 Hz changes from -2.764 dB for
a -5.9 dB cut to -1.897 dB for a -6 dB cut. Smoothing that transition or
extrapolating a linear Q fit produces an incorrect preview. Positive-gain
unity behavior is established by the device captures.

The tracked fixture includes 22 notch responses spanning -0.1 to -20 dB,
requested Q 0.5/1/2/4, and centers at 500/1000/2000/4000 Hz. These are
selected combinations, not a full Cartesian sweep. All 59 measured
frequencies (50–15000 Hz) are retained per response. Maximum error is
0.017 dB / 0.190 degrees. Two initial shallow-cut recordings had transient
noise and were repeated; the fixture uses the repeat recordings for those
settings.

Slope 0/1 have the same measured notch behavior; other slope values remain
explicitly unresolved in the preview. Wire serialization has not changed.

## Bessel high-pass: calibrated sections

An ideal `scipy.signal.bessel(..., norm="mag")` high-pass matches the device's
cutoff attenuation but has the wrong transition shape and phase. Shifting
that prototype's cutoff cannot correct both at once.

The implemented model cascades first- and second-order high-pass sections.
Each second-order section uses `H(s) = s²/(s² + w0/Q*s + w0²)`.
Even orders share a section frequency but have distinct section Q values;
odd orders additionally use a first-order section at a different frequency.
For each section, multiply the configured frequency by its table scale
**before** digital prewarping. Scaling an already-prewarped frequency
introduces avoidable errors at higher cutoffs.

The table in `response.py` was fitted solely to the 1 kHz captures for
6–48 dB/oct. It minimizes real and imaginary complex-log transfer errors,
using normalized frequencies between 0.5 and 8. The first-order case uses
the standard analytic coefficients without fitting. Independent cutoffs
were reserved for validation; they did not adjust the table.

| Slopes | Independent validation cutoffs |
| --- | --- |
| 12 dB/oct | 200, 500, 2000, 5000, 6000 Hz |
| 24 dB/oct | 200, 5000, 6000 Hz |
| 18, 30, 36, 42, 48 dB/oct | 3000 Hz |

The fixture retains 23 high-pass responses, including calibration and
validation recordings. Error is checked between `fc/2` and
`min(8*fc, 15000 Hz)`: maximum 0.018 dB / 0.147 degrees. Deep-stopband bins
are excluded because their noise-dominated phase cannot validate filter
coefficients. These bounds do not certify other sample rates, every cutoff,
or different hardware/firmware. The model uses 96 kHz. Bessel low-pass
continues to use the existing magnitude-normalized model.

## Odd-order Linkwitz–Riley: firmware-specific sections

The LA548 implements all eight slope selections for family 1. Conventional
Linkwitz–Riley crossovers are even order, but live HP/LP sweeps show that the
four odd selections (6, 18, 30, and 42 dB/oct) are active and are
approximately -6 dB at the configured crossover.

The model uses the following constants, checked against simultaneous-reference
audio measurements. The 6 dB mode is a first-order filter whose pole is scaled
by 1.732 for HP and by its reciprocal for LP. Higher odd orders add an unshifted
first-order section and these second-order damping constants, where
`Q = 1/(2*damping)`:

| Display slope | Damping constants |
| --- | --- |
| 18 dB/oct | 0.70422535211 |
| 30 dB/oct | 0.62, 0.62 |
| 42 dB/oct | 0.453134315, 0.92592592593, 0.453134315 |

`tests/fixtures/odd_lr_measurements.json` retains measured HP and LP points
for every odd slope. Across retained bins above -38 dB, the implementation
matches within 0.2 dB and 1.5 degrees. This evidence is specific to the
96 kHz LA548 firmware; the section shapes should not be generalized from
the Linkwitz–Riley name alone.

## Shelves: order and phase

Output-channel measurements confirm `slope=0` selects a first-order shelf
and `slope=1` a second-order shelf. The stored Q does not affect the shelf
shape (tested at requested Q 0.5/1/2), so a Q-dependent second-order RBJ
shelf mispredicts both orders. For peaking EQ and AllPass1, slope 1 instead
gives the same response as slope 0; the preview models both settings.

Let `G = 10**(abs(gain_db)/20)` and define the normalized digital frequency
variable `s = j*tan(pi*f/fs)/tan(pi*fc/fs)`. For nonzero gain, the measured
first-order transfers are:

| Shelf | Boost | Cut |
| --- | --- | --- |
| Low | `(G-s)/(1+s)` | `(1-s)/(G+s)` |
| High | `(1-G*s)/(1+s)` | `(1-s)/(1+G*s)` |

The zeros are in the right half plane: these are non-minimum-phase shelves.
The code preserves the measured phase rather than substituting a familiar
minimum-phase shelf with similar magnitude. For both signs of gain, phase
approaches -180 degrees at high frequencies. At exactly zero gain, the
device bypasses the filter instead of taking the nonzero-gain limit.
At fc=1 kHz, a ±0.1 dB low shelf measured -89.68 degrees at fc, whereas
the zero-gain setting measured approximately zero degrees.

For second-order boosts, the denominator is `s² + sqrt(2)*s + 1`.
The numerator is `s² + sqrt(2*G)*s + G` for a low shelf, or
`G*s² + sqrt(2*G)*s + 1` for a high shelf. Cuts invert the boost transfer.
Zero gain is unity. These shelves are minimum phase and do not use stored Q.

For either order, fc is the prototype corner, not the half-gain midpoint.
A +12 dB shelf measures about +9.255 dB at fc. For a first-order low shelf
with a 1 kHz setting, the half-gain point is near 2 kHz. Matching only the
asymptotic gains would miss both the frequency and phase errors.

`tests/fixtures/shelf_measurements.json` retains 21 recorded responses:
both shelf types/orders, boosts/cuts, zero/tiny gains, a Q sweep, independent
3 kHz cases after the 1 kHz characterization, and slope-1 peaking/allpass
controls. Across 59 frequencies from 50 to 15000 Hz, the model's
maximum error is 0.022 dB / 0.151 degrees. This is output-channel evidence;
it does not independently characterize input shelves or unsupported slopes.

All three shelf measurement batches restored the complete settings snapshot,
including the original disabled EQ band. A final independent read verified
restoration.

## Reproduction and regression tests

`tests/fixtures/filter_measurements.json` contains the measured frequency,
gain, and phase values, parameters after wire quantization, and explicit
tolerances. No expected value is generated from the production model. Tests
run without a DSP or local capture files.

```sh
.venv/bin/python -m tools.calibrate_bessel_hp
.venv/bin/python -m pytest -q tests/test_filter_measurements.py tests/test_shelf_measurements.py tests/test_response.py
```

The calibration command prints the production table from the tracked 1 kHz
measurements, to six decimal places. It never writes files or contacts
hardware. The regression set includes independent cutoff frequencies for
every calibrated slope and tests both sides of the notch's -6 dB boundary.

## Hardware method and restoration

The measurement uses a loopback that drives DSP inputs A/B with the same
signal and captures outputs 1/2 simultaneously. A deterministic 59-tone
multisine at peak amplitude 0.06 is captured at 48 kHz. A stationary two-second spectral
ratio (test/reference), divided by the bypass baseline ratio, removes common
capture timing and the existing reference-channel response.

Only output 2's first EQ or high-pass block was changed, using validated
Client setters. Each write was checked for unexpected snapshot byte changes.
All four probe batches restored the full 2464-byte settings snapshot exactly;
a final independent device read also matched the initial snapshot.
