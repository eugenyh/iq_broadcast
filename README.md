[English](README.md) | [Русский](README.ru.md)

# HF Test RF Signal Generator

A web interface for transmitting a library of test IQ signals in the HF band
through a **HackRF One**. Pick a signal from the drop-down list → set the
frequency/gain → press "Start" → the signal goes on air, while the browser
draws its spectrum, waterfall and playback progress in real time. It also
provides a spectrum preview without transmitting, a technical data sheet for
every signal (modulation, baud rate, FEC, etc.), and signal exchange between
program instances via bundle files with import straight from the server's disk.

The library currently contains **91 signals**, from the classics (STANAG-4285,
PACTOR, SITOR) to about fifty modes from the WaveCom archive (MFSK/PSK
families, military ARQ, fax, Hellschreiber). The full list is in the
[appendix](#appendix-full-signal-list) at the end of this document.

The look is a black-and-green terminal theme.

> **Note:** the web interface is in Russian. UI element names are given below
> in English, with the original Russian label in parentheses where useful.

---

## Table of contents

1. [Project structure](#project-structure)
2. [Architecture and data flow](#architecture-and-data-flow)
3. [How it works, step by step](#how-it-works-step-by-step)
   - [Signal storage format](#1-signal-storage-format)
   - [WAV to IQ conversion](#2-wav-to-iq-conversion-wav_to_iq_librarypy)
   - [Test tone generation](#3-test-tone-generation-generate_sinepy)
   - [Spectrum cache](#4-spectrum-cache-spectrum_cachepy)
   - [Transmission](#5-transmission-hackrf_txpy)
   - [Live spectrum and preview](#6-live-spectrum-and-preview)
   - [Playback progress](#7-playback-progress)
   - [Signal technical spec](#8-signal-technical-spec-signal_specpy)
   - [Bundle export and import](#9-bundle-export-and-import)
4. [library.json schema](#libraryjson-schema)
5. [Backend API](#backend-api)
6. [Installation and running](#installation-and-running)
7. [Adding and annotating signals](#adding-and-annotating-signals)
8. [Exchanging signals between machines](#exchanging-signals-between-machines)
9. [Web interface](#web-interface)
10. [Porting to Raspberry Pi 4 (8 GB) with a touchscreen](#porting-to-raspberry-pi-4-8-gb-with-a-touchscreen)
11. [Debugging history and known pitfalls](#debugging-history-and-known-pitfalls)
12. [TX level safety](#tx-level-safety)
13. [Appendix: full signal list](#appendix-full-signal-list)

---

## Project structure

```
iq_broadcast/
  backend/
    main.py              # FastAPI: all endpoints, broadcast/import state, threads
    hackrf_tx.py         # wrapper around hackrf_transfer (start/stop/monitor the process)
    spectrum_cache.py    # FFT computation + caching, used by both live and offline paths
    library_utils.py     # reading/writing library.json (shared by converters and main.py)
    signal_spec.py       # description of the "data sheet" technical fields of a signal
  tools/
    wav_to_iq_library.py # converter: WAV -> .cs8 + library entry + spectrum cache
    generate_sine.py     # pure-tone generator -> .cs8 + library entry + cache
    export_bundle.py     # packs library signals into a .tar for handing to another user
    annotate_signal.py   # edits the technical data sheet of an existing signal without reconverting
  library/
    library.json         # metadata of all signals
    <id>.cs8             # the IQ files themselves (int8, ready for HackRF)
    spectrum_cache/
      <id>.npz           # precomputed spectrum (FFT frames), see below
    _import_tmp/         # temporary folder for unpacking bundles (see below for why it is here)
  frontend/
    index.html           # the whole web interface: markup + styles + JS in one file
  requirements.txt
  README.md
```

---

## Architecture and data flow

```
                         ┌─────────────────────────┐
                         │ library/library.json    │
                         │ library/<id>.cs8        │
                         │ library/spectrum_cache/ │
                         └───────────┬─────────────┘
                                     │ reads
                    ┌────────────────┴─────────────────┐
                    │                                  │
          ┌─────────▼─────────┐              ┌─────────▼─────────┐
          │  backend/main.py  │              │  tools/*.py       │
          │  (FastAPI server) │              │ (converters,      │
          │                   │              │  annotation,      │
          │                   │              │  bundle export)   │
          └──┬──────────────┬─┘              └───────────────────┘
             │              │
    POST/GET │              │ WebSocket
             │              │
     ┌───────▼──────┐  ┌────▼─────────────────┐
     │ hackrf_tx.py │  │ spectrum_cache.py    │
     │ (subprocess  │  │ (computing/reading   │
     │  hackrf_     │  │  FFT frames)         │
     │  transfer,   │  └───────────┬──────────┘
     │  stderr      │              │
     │  monitoring) │              │
     └───────┬──────┘              │
             │                     │
     ┌───────▼───────┐      ┌───────▼────────┐
     │  HackRF One   │      │  frontend/     │
     │ (real TX)     │      │  index.html    │
     └───────────────┘      │  (spectrum,    │
                            │   waterfall,   │
                            │   progress,    │
                            │   controls)    │
                            └────────────────┘
```

There are two fundamentally independent data paths:

- **Transmit path**: `main.py` launches `hackrf_transfer` (via
  `hackrf_tx.py`), which reads the `.cs8` file from disk and pushes it into
  the HackRF. There is no Python on the path of the samples themselves — this
  turned out to be critical for stability (see the
  [debugging history](#debugging-history-and-known-pitfalls)). Separately,
  `hackrf_tx.py` watches the stderr of that process to tell real work apart
  from "alive but hung".
- **Visualization path**: a separate thread reads the same `.cs8` (or the
  ready-made cache) and computes/sends the spectrum over WebSocket. It is not
  tied to the transmit path in any way — a failure of one does not affect the
  other, and you can even preview a completely different signal during a real
  transmission.

---

## How it works, step by step

### 1. Signal storage format

Each signal in the library is a ready-made **int8 IQ file** (`.cs8`,
interleaved I,Q, one byte per component, range -128..127) already at the
sample rate `hackrf_transfer` will read it at (2 MHz by default). There is no
intermediate "master format" and no rendering at playback time — conversion
to the HackRF format happens once, when a signal is added to the library, by
the tools in `tools/`.

It was not always this way — originally the library stored a compact
"master" file at a low rate (200 kHz, int16), and resampling for the HackRF
happened on the fly on every playback start. That scheme was abandoned:
rendering took a noticeable amount of time (about a minute for a 3-minute
signal) right before the transmission started. Since conversion is needed
once anyway, it is more sensible to write the target format right away and
not spend time on every `play`. The price is larger library files (hundreds
of MB for a long recording), but it is a one-off cost when adding a signal,
not on every playback.

### 2. WAV to IQ conversion (`wav_to_iq_library.py`)

Takes a mono WAV recording of an HF signal as it sounds in headphones after
SSB demodulation (modem tones at audio frequencies, usually 0.3–3 kHz — fits
STANAG-4285, ALE, PACTOR, RTTY, PSK31, etc.) and turns it into a complex IQ
signal ready for transmission through an SDR. Pipeline steps:

1. **Read the WAV**, convert to mono (the first channel is used if there are
   several) and normalize the amplitude to the range [-1, 1].
2. **Low-pass filter (Butterworth, order 6)** before the Hilbert transform —
   default cutoff 3500 Hz. Removes noise near the Nyquist frequency of the
   source recording: without this step, when the analytic signal is built
   (next step), such noise leaks as a mirror artifact onto negative
   frequencies because of the Gibbs effect on a finite-length transform.
3. **Hilbert transform** — builds the analytic (complex) signal from the real
   audio signal. This is the key step: a real recording by itself carries no
   information about which side of the carrier the signal should end up on
   when transmitted; the Hilbert transform restores it, reproducing the same
   sideband (USB/LSB) the signal was originally received in.
4. **Resampling** from the WAV's original rate (usually 8–48 kHz) to the
   library's target rate (2 MHz by default) — block-wise linear
   interpolation, without building a full polyphase FIR filter (with such a
   huge resampling ratio, hundreds of times, a full FIR would cause an
   unacceptable memory spike).
5. **Low-pass filter AFTER resampling** (Butterworth, order 8, via SOS —
   second-order sections, not the ordinary `(b, a)` representation: at such
   a high order and low normalized cutoff frequency `(b, a)` is numerically
   unstable and yields `NaN` at the output). Mandatory: plain linear
   interpolation at a resampling ratio of hundreds creates spurious images
   at frequencies of the form `-(fs_source - f_signal)` — verified
   empirically on several signals; the image frequency predicted by this
   formula matched the observed one to within hundreds of hertz. Without this
   step the artifact was 20–25 dB below the peak — quite visible on the
   spectrum, and it really went on air during transmission. The filter is
   implemented with state (`zi`) carried across blocks, so there are no
   clicks at block boundaries.
6. **Scaling and writing** — the amplitude is multiplied by the `--gain`
   factor (0..1, default 0.7, to leave headroom against clipping when
   converting to 8 bits) and written as interleaved int8 I/Q.
7. **Library registration** — metadata is written to `library.json`
   (via `library_utils.register_signal`, which replaces an entry with the
   same `id` if one already exists).
8. **Spectrum cache build** — `spectrum_cache.build_cache_for_signal()` is
   called automatically (see below), no separate step is needed.

Full list of command-line parameters:

| Flag | Default | Meaning |
|---|---|---|
| `input_wav` | — | path to the source WAV (required) |
| `--id` | — | unique library ID (required) |
| `--name` | — | display name (required) |
| `--freq` | — | recommended transmit frequency, Hz (required) |
| `--description` | `""` | signal description |
| `--library` | `../library` | path to the library folder |
| `--sample-rate` | `2000000` | sample rate of the output file, Hz |
| `--gain` | `0.7` | amplitude scale 0..1 |
| `--lowpass` | `3500.0` | cutoff of the pre-Hilbert low-pass filter, Hz (`0` — disable) |
| `--tx-vga-gain` | `20` | default HackRF TX VGA gain for this signal, 0–47 dB |
| `--amp-enable` | off | enable the HackRF built-in amplifier (+14 dB) by default |
| `--sideband` | `usb` | sideband of the source recording (`usb`/`lsb`) — informational field |
| `--spectrum-freq-min-khz` | `-5.0` | lower edge of the spectrum window in the UI, kHz |
| `--spectrum-freq-max-khz` | `5.0` | upper edge of the spectrum window, kHz |
| `--spectrum-db-min` | `-100.0` | lower edge of the amplitude scale on the plot, dB |
| `--spectrum-db-max` | `0.0` | upper edge of the amplitude scale, dB |
| `--loop` | on | loop playback by default for this signal |
| `--modulation`, `--tone-count`, `--baud-rate`, `--shift-hz`, `--bandwidth-hz`, `--bitrate-bps`, `--encoding`, `--fec`, `--interleaving` | all `None` | technical data sheet of the signal, see [section 8](#8-signal-technical-spec-signal_specpy) |

Example:
```bash
cd tools
python3 wav_to_iq_library.py path/to/signal.wav \
    --id my-signal \
    --name "Signal name" \
    --freq 7000000 \
    --description "..." \
    --modulation "PSK" --baud-rate 100 \
    --library ../library
```

### 3. Test tone generation (`generate_sine.py`)

A separate, much simpler path for pure calibration signals — no WAV input,
no low-pass filter or Hilbert transform. It generates a complex exponential
directly:

- With `--offset-khz 0` — a pure carrier: a constant vector `I = amplitude,
  Q = 0` for the whole duration. There cannot be a seam when looping (the
  signal is constant).
- With a non-zero offset — `I(t) + jQ(t) = amplitude · e^(j·2π·offset·t)`.
  The duration is automatically adjusted to an **integer number of periods**
  of the tone, so that when the file is looped (`hackrf_transfer -R`) there
  is no click at the joint (the last sample of the file smoothly continues
  into the first).

The default offset from center is **+1 kHz, not 0** — direct-conversion
transmitters (HackRF included) often show a spurious DC spike from LO
leakage right at the carrier; a tone off to the side cannot be confused with
it on a spectrum analyzer.

The technical data sheet is filled with sensible defaults unless overridden
explicitly: `modulation` = "Unmodulated carrier (CW)" at zero offset,
otherwise "Unmodulated tone"; `tone_count` = 1; `fec` and `interleaving` =
"none".

Command-line parameters:

| Flag | Default | Meaning |
|---|---|---|
| `--id` | — | unique ID (required) |
| `--name` | `"Test tone"` | display name |
| `--freq` | — | center transmit frequency, Hz (required) |
| `--offset-khz` | `1.0` | tone offset from center, kHz (`0` = pure carrier) |
| `--amplitude` | `0.8` | amplitude 0..1 |
| `--duration` | `2.0` | approximate duration, s (the actual one is adjusted to an integer number of periods) |
| `--sample-rate` | `2000000` | sample rate, Hz |
| `--library` | `../library` | path to the library |
| `--tx-vga-gain` | `20` | default TX VGA gain |
| `--amp-enable` | off | HackRF amplifier by default |
| `--description` | auto-generated | description |
| `--spectrum-freq-min-khz` / `--spectrum-freq-max-khz` | chosen automatically (with margin around the tone) | spectrum window |
| `--spectrum-db-min` / `--spectrum-db-max` | `-100.0` / `0.0` | amplitude scale |
| technical data sheet (see section 8) | see above | same as `wav_to_iq_library.py` |

Example:
```bash
python3 generate_sine.py --id carrier-10mhz --name "10 MHz carrier" \
    --freq 10000000 --offset-khz 0 --library ../library
```

### 4. Spectrum cache (`spectrum_cache.py`)

Computing an FFT over the whole signal file is not the cheapest operation,
and with looped playback it would be repeated on every lap for no reason at
all (the signal in the file is always the same). So the spectrum is computed
**once, when the signal is added to the library**, and cached:

- `iter_raw_frames(file)` — reads the file in windows of `CHUNK_SAMPLES =
  65536` samples, computes an FFT with a Blackman-Harris window (low
  sidelobes) on each window and yields the spectrum in dB. The last
  incomplete block at the end of the file is discarded. This generator is
  **shared code** used both by the cache builder and by live computation (if
  there is no cache) — so the behavior of the two is guaranteed not to
  diverge.
- `build_cache_for_signal(signal_meta, library_dir)` — runs `iter_raw_frames`
  over the whole file, crops every frame by frequency
  (`spectrum_freq_min_khz`/`max_khz` from the signal metadata) and saves
  everything to `library/spectrum_cache/<id>.npz` (a compressed numpy
  archive: frequency array, frame matrix `[n_frames × n_points]`, plus
  metadata for validity checking).
- `load_cache_if_valid(signal_meta, library_dir)` — on playback, checks that
  the cache exists, is not older than the `.cs8` itself (by mtime) and
  matches the signal's current `sample_rate`, FFT window size and crop
  limits. On any mismatch it silently returns `None`, without exceptions,
  and the consumer code simply computes live.

The cache is **not put into bundles on export, nor anywhere else** — it is
regenerated in place (including automatically when importing a bundle), so
as not to depend on the version of `spectrum_cache.py` on another machine.

Averaging (smoothing the spectrum between frames, exponential, `AVG_ALPHA =
0.25`) is **not stored** in the cache — raw frames are cached, and averaging
is done on the fly during playback (in `main.py`). This keeps the visual
behavior identical whether the cache or live computation is used, including
the smooth transition at the loop seam.

The cache can be rebuilt manually, without reconverting the signal itself
(for example, if `CHUNK_SAMPLES` was changed in the code, or
`spectrum_freq_min/max_khz` was changed via `annotate_signal.py` — see
section 8):
```bash
cd backend
python3 spectrum_cache.py                # rebuild for ALL library signals
python3 spectrum_cache.py stanag-4285    # only for one
```

The cache size is negligible compared to the IQ file itself: for STANAG-4285
(172 s, 2 MHz) the cache is **4.15 MB** against **688 MB** for the `.cs8`.

### 5. Transmission (`hackrf_tx.py`)

`HackRFTransmitter` is a thin wrapper around the standard `hackrf_transfer`
utility (not the Python bindings of libhackrf — the utility from the HackRF
developers holds real-time transmission more reliably).

**Start** (`start()`):
```
hackrf_transfer -t <file> -f <frequency> -s <sample_rate> -x <tx_vga_gain> -a <0|1> [-R]
```
- `-t <file>` — reads the ready `.cs8` straight from disk (not a stream from
  Python — see the [debugging history](#debugging-history-and-known-pitfalls)
  for why).
- `-R` is added only if `loop=True` for this signal run. Without it,
  `hackrf_transfer` plays the file once and exits by itself with code `0` —
  this is recognized as a normal completion, not an error.
- Before starting — `_preflight_check()`: tries `hackrf_info` from the same
  package as `hackrf_transfer` (looked up alongside it on the path), with
  several attempts — right after the previous transmission is stopped, the
  device sometimes does not manage to release instantly.
- On Windows the process is launched in **its own separate hidden console**
  (`CREATE_NEW_CONSOLE` + `STARTUPINFO` with `SW_HIDE`) — technically needed
  for correct stopping (see below), but no window is visible.

**Stop** (`close()` → `_graceful_stop()`): a hard `terminate()` will not do —
on Windows it is `TerminateProcess()`, which gives `hackrf_transfer` no
chance to release the device properly (`hackrf_stop_tx() → hackrf_close() →
hackrf_exit()`), so the HackRF stays in the "transmitting" state at the
firmware level, and the next `hackrf_open()` may fail until the USB is
physically reconnected. Instead:
- on Linux/macOS — an ordinary `SIGINT` (same as pressing Ctrl+C);
- on Windows — sending `CTRL_C_EVENT` through a **separate short-lived
  helper process** (`python -c "..."`) that attaches to the console of
  `hackrf_transfer` (`AttachConsole`), sends the event and exits at once.
  Doing this in a separate process rather than in the server itself is
  essential: `AttachConsole`/`FreeConsole` operate at the level of the whole
  process, not a thread, and if done directly in a thread inside `uvicorn`,
  **the entire** server (including HTTP request handling in other threads)
  is left without a console for a short time and may hang.
- if the graceful stop did not work within the allotted time — only then
  `terminate()`/`kill()` as a last resort.

**Hang monitoring** (`check_alive()`): a process can be formally alive
(`proc.poll()` returns `None`) but not actually be pushing data to USB — for
example, after several successful cycles it suddenly goes silent without a
single error (observed in practice). `poll()` does not catch that — the
**stderr** of `hackrf_transfer` itself must be monitored (stderr, not stdout
— with `-t <file>` nothing at all is written to stdout; all diagnostic
output, including periodic status lines like `X MiB / Y sec = ...`, goes to
stderr). A background thread (`_read_stderr`) reads these lines one by one
and updates a last-activity timestamp; if silence in stderr lasts longer
than `STALL_TIMEOUT_SEC = 5.0` seconds, `check_alive()` raises an exception
with diagnostics (the last lines of output) even if the process itself has
not exited. On normal completion with code `0` (file played to the end
without looping) this is still not an error.

**Cycle tracking** (`cycle_count`, `cycle_start_time`): the line `"Input
file end reached. Rewind to beginning."` in stderr means that
`hackrf_transfer` really has started a new pass through the file — a fact,
not an estimate. Used for an honest progress bar, see
[section 7](#7-playback-progress).

If the `hackrf_transfer` binary is not found in `PATH` (and is not given
explicitly via the `HACKRF_TRANSFER_BIN` environment variable), the class
switches to **simulation** mode. This mode is simpler than it might seem:
`start()` in this case just prints a message to the log and returns
immediately, without launching any process and without any pauses at all;
`check_alive()` always returns `True`, also without any delay. Because of
this, "playback" in simulation mode does not stop by itself — the polling
loop in `main.py` (`while ...: check_alive(); time.sleep(1.0)`) keeps
spinning until the user presses "Stop" themselves, even if the signal has
`loop=False`.

The illusion that "the signal is really playing" in this mode is created
**not** by this class but by a completely independent spectrum thread
(`spectrum_worker`, section 6) — it reads the real `.cs8` file and keeps
real playback pace through `stream_spectrum()`, without knowing that
`hackrf_tx.py` exists and without looking at its `simulate` state. That is
exactly why the spectrum and waterfall behave equally plausibly with real
transmission and without it — whereas the progress bar in simulation mode
works only from a rough time estimate (`cycle_start_time` inside
`HackRFTransmitter` is never set in this mode, because the code that sets it
lies after the `return` point in `start()`).

Convenient for interface development on a machine where `hackrf_transfer` is
not installed at all.

**This is not the same as "HackRF is physically not connected".** The check
is `shutil.which(HACKRF_TRANSFER_BIN) is None`, i.e. it only looks at the
presence of the executable itself, not the physical device. If
`hackrf_transfer` is installed (present in `PATH`) but the HackRF itself is
not connected (or is busy/not responding), simulation is NOT enabled:
`hackrf_transfer` is really launched and fails with a genuine
no-device error, which ends up in `/status.last_error`. To view a signal's
spectrum without a connected device in that case, use the **"Preview"**
button instead of "Start": it does not touch `hackrf_transfer` at all (see
section 6), so it works regardless of whether the HackRF is connected.

### 6. Live spectrum and preview

Both tasks use the same function `stream_spectrum(signal_meta, emit_fn,
stop_event)` in `main.py` — it either plays back frames from the cache in a
loop or (if there is no cache) computes live through `iter_raw_frames`, with
the same averaging and the same real pace (the pause between frames is
computed from the actual processing time so as not to accumulate drift). The
only differences between the two modes of use are where the computed frames
go and what stops them:

- **Real broadcast** (`spectrum_worker`) — frames go out via
  `state.broadcast()`, i.e. to all clients connected to `/ws/spectrum`;
  stopped by the common `state.stop_flag` (the same one that stops the
  transmission itself through HackRF).
- **Preview** (`/ws/preview/{signal_id}`) — each WebSocket connection gets
  its own thread and its own `stop_event`, and frames go straight to that
  one connection. Completely independent of `PlaybackState` — does not touch
  the HackRF and does not affect the real broadcast (if one is running at
  the moment). You can transmit one signal and view the spectrum of a
  completely different one at the same time — they do not interfere. The
  thread ends when the client closes the WebSocket (closes the preview modal
  window in the interface).

Before sending, the spectrum is cropped by frequency according to the
signal's `spectrum_freq_min_khz`/`max_khz` — the client receives an already
compact set of points (usually 150–300 depending on window width), and the
axis labels on the plot are taken from the actual bounds of that data.

### 7. Playback progress

The progress indicator under the status shows the position within the
current pass through the file and the cycle number (if looping is on). The
data source is a **time-based estimate with self-correction**, not an exact
read of the file position (we have no access to the internal state of
`hackrf_transfer`, which reads the file by itself, bypassing Python):

- `state.playback_start_time` — the start moment, used for the very first,
  not yet confirmed estimate.
- `tx.cycle_start_time` / `tx.cycle_count` in `hackrf_tx.py` — updated on
  every real detection of `"Rewind to beginning"` in stderr (see section 5).
  This is no longer an estimate but a fact: as soon as at least one
  confirmed cycle has happened, `/status` starts returning
  `cycle_position_sec` and `cycle_number` synchronized with reality on every
  pass — without accumulating drift over long sessions.
- The frontend (`updateProgress()`) prefers the confirmed data, falling back
  to the time estimate (`elapsed_sec % duration_sec`) only until no cycle has
  happened yet (the very beginning of the first pass) or in simulation mode
  without a real `hackrf_transfer`.

### 8. Signal technical spec (`signal_spec.py`)

Besides the basic fields (frequency, duration, etc.), each signal can carry a
technical data sheet — modulation, rate, FEC and so on. All the fields are
shared by the converters and `main.py` (to avoid duplicating the list in
several places) and are described in `backend/signal_spec.py`:

| Field | Meaning |
|---|---|
| `modulation` | Modulation type (FSK, PSK, MFSK, AFSK, GMSK, etc.) — the most important field |
| `tone_count` | Modulation order / number of tones (2 = BFSK, 4, 8, 16 = MFSK16, etc.) — for FSK/MFSK |
| `baud_rate` | Symbol rate, baud |
| `shift_hz` | Shift/tone spacing, Hz — for FSK/MFSK |
| `bandwidth_hz` | Nominal signal bandwidth per the specification, Hz |
| `bitrate_bps` | Actual data rate, bit/s — if different from the baud rate |
| `encoding` | Encoding/alphabet (Baudot, ASCII, Varicode, tone dialing, etc.) |
| `fec` | Error correction: present/absent, type (Viterbi, Reed-Solomon, etc.) |
| `interleaving` | Interleaving: present/absent (fading resistance) |

All the fields are optional (`None` by default) and purely informational —
they affect neither transmission nor the spectrum. In the interface the
details card shows only the rows that have data — a signal without a data
sheet simply does not show this block, no visual clutter.

**Filling in on conversion** — both converters accept all these fields as CLI
flags (see the tables in sections 2 and 3).

**Editing an existing entry** — `tools/annotate_signal.py`: updates the data
sheet fields without reconverting the IQ file and without rebuilding the
spectrum cache (these fields affect neither the file nor the spectrum).
Specify only the flags you want to change — the other fields of the entry
stay as they are:
```bash
cd tools
python3 annotate_signal.py --id stanag-4285 \
    --modulation "PSK (BPSK/QPSK/8PSK)" --baud-rate 2400 \
    --bandwidth-hz 3000 --fec "Convolutional (Viterbi)" --interleaving "yes"
```

Values in the current library are taken only where reliable sources were
found (mode creators' websites, ITU recommendations, sigidwiki, etc.) —
where there was no certainty, the field is left empty rather than filled
with a plausible guess. See the [appendix](#appendix-full-signal-list) for
the full table.

### 9. Bundle export and import

The format for transferring signals between different installations of the
program is a `.tar` archive:
```
bundle.tar
├── manifest.json        — list of signal metadata (same format as library.json)
└── signals/
    ├── <id-1>.cs8
    └── <id-2>.cs8
```
The spectrum cache is **not put into the archive** — it is regenerated in
place at the recipient, so as not to depend on the version of
`spectrum_cache.py` on another machine.

**Export** (`tools/export_bundle.py`) — reads `library.json`, and for each
requested (or all) signal copies its `.cs8` into the archive under
`signals/<file>` and assembles the metadata list into `manifest.json` inside
the same archive.

**Import — only from the server's disk; browser upload is gone.** Import was
originally implemented as an ordinary file upload in the browser
(`multipart/form-data`), but in practice this proved unreliable: for bundles
of hundreds of MB the browser/starlette sometimes could not parse such a
large request body (`"There was an error parsing the body"`), so the import
failed for no clear reason. Since the server and the browser run on the same
machine anyway, the solution is to read the `.tar` directly from disk (USB
stick, network folder), without pushing the data through HTTP at all:

- **`POST /import-local?path=...`** — starts the import in a background
  thread and immediately returns `{"status": "started"}` without waiting for
  completion.
- **`GET /import/status`** — progress: the current step (`"Copying '<id>'
  (N/M)..."`, `"Building spectrum cache '<id>' (N/M)..."`), the
  `done`/`total` counter, and at the end — the result (`imported: [...]`) or
  an error.
- **`GET /import/browse`** — a simple file browser for the interface:
  without a path it returns the list of **allowed roots**, with a path — the
  contents of the folder (only subfolders and `.tar` files; everything else
  is not shown).

**Allowed roots** (`_get_allowed_roots()`) — set by the `IMPORT_ALLOWED_ROOTS`
environment variable (comma-separated paths); without it — reasonable
defaults: all drives on Windows, the standard USB auto-mount points on
Linux/Pi (`/media`, `/mnt`, `/run/media`), and if those do not exist either —
the whole root `/`. The restriction is checked at two levels — in the file
browser itself (you are not allowed to go outside the allowed root) and
again on `/import-local` (even if the API is called directly, bypassing the
interface). Example of launching with a restriction to a specific folder:
```bash
IMPORT_ALLOWED_ROOTS=/media/usb uvicorn main:app --host 0.0.0.0 --port 8000
```

**Temporary folder for unpacking** — deliberately created **inside
`library/`** (`library/_import_tmp`), not in the default system temp. On
Windows the system temp is almost always on drive `C:`, and if the library
itself (and space for it) is on another drive, unpacking a large bundle can
run out of space on `C:` even when the target drive has plenty. Creating the
temporary folder next to `library/` guarantees that unpacking happens on the
same drive the files are then actually copied to. On server startup this
folder is cleaned just in case — in case the previous run crashed in the
middle of unpacking.

**What happens on import** (common logic — `_import_bundle_from_path`):
1. Unpacking with a safety check (`_safe_extract`): for every archive member
   it checks that the resulting path does not leave the extraction directory
   (protection against **path traversal** — `../../../etc/...`, via
   `Path.relative_to()` — not by comparing strings with a hard-coded `/`
   separator, which broke on Windows, where the real separator is `\`), and
   that there are no symbolic links among the members. On Python 3.12+ the
   standard filter `tarfile.extractall(filter="data")` (PEP 706) is
   additionally used as a second layer of protection.
2. `manifest.json` is read, and for each entry the required fields (`id`,
   `name`, `file`, `sample_rate`, `recommended_freq_hz`) are checked — if
   even one is missing, an error is returned naming which fields are
   missing. Missing optional fields (including the whole technical data
   sheet from section 8) are filled with defaults.
3. The signal's `.cs8` file is copied into `library/` under the name
   `<id>.cs8` (the name is always taken from `id`, not from an arbitrary
   file name from the manifest — additional protection against path
   spoofing).
4. The entry is added to `library.json` (`register_signal` — replaces an
   existing entry with the same `id` if there was one; the result is marked
   `updated: true`/`false` accordingly).
5. The spectrum cache is **rebuilt** in place.

**In the interface**: the "+ Import" button opens a modal window with a file
browser; clicking a `.tar` starts the import, and the window shows a
progress bar with the current step. While the import runs, the file list in
the window is locked (clicks do not go through), the close button is
inactive, and closing by clicking the backdrop or by Escape is blocked too —
so that the process cannot be interrupted by accident or confused by
clicking another file.

Any failure on this path — including an unexpected FastAPI validation error
that happens BEFORE the endpoint code even gets to run — is guaranteed to be
returned in the single format the frontend expects, `{"error": "..."}`,
thanks to two global exception handlers (`@app.exception_handler(Exception)`
and, separately, `@app.exception_handler(RequestValidationError)` — FastAPI
has its own higher-priority handler for `RequestValidationError`, the
generic one does not catch it, so a separate one is needed).

---

## library.json schema

An array of objects, each describing one signal:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | string | yes | unique identifier, also used as the file name (`<id>.cs8`) |
| `name` | string | yes | display name in the interface |
| `file` | string | yes | name of the `.cs8` file in the `library/` folder |
| `sample_rate` | number | yes | sample rate of the file, Hz |
| `recommended_freq_hz` | number | yes | recommended transmit frequency, Hz (overridable in the interface before start) |
| `tx_vga_gain` | integer 0–47 | no (20) | default HackRF TX VGA gain, dB |
| `amp_enable` | bool | no (false) | whether to enable the HackRF built-in amplifier (+14 dB) by default |
| `sideband` | string | no | `usb`/`lsb`/`n/a` — informational field, does not affect transmission |
| `gain` | number 0..1 | no | amplitude baked into the samples themselves at conversion — **not changeable on the fly**, only by recreating the file |
| `loop` | bool | no (true) | whether to loop playback by default |
| `description` | string | no | description shown in the details card |
| `duration_sec` | number | no | file duration, seconds (for display) |
| `spectrum_freq_min_khz` | number | no (-5.0) | lower edge of the spectrum window on the plot, kHz |
| `spectrum_freq_max_khz` | number | no (5.0) | upper edge of the spectrum window, kHz |
| `spectrum_db_min` | number | no (-100.0) | lower edge of the amplitude scale, dB |
| `spectrum_db_max` | number | no (0.0) | upper edge of the amplitude scale, dB |
| `modulation`, `tone_count`, `baud_rate`, `shift_hz`, `bandwidth_hz`, `bitrate_bps`, `encoding`, `fec`, `interleaving` | see section 8 | no (`None`) | technical data sheet, see [section 8](#8-signal-technical-spec-signal_specpy) |

The fields `tx_vga_gain`, `amp_enable`, `loop`, `recommended_freq_hz` are
**default** values; the interface always lets you override them before
starting a specific transmission, without touching `library.json` itself.

---

## Backend API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/signals` | list of all signals from `library.json` |
| `GET` | `/status` | current state: what is playing, phase (`starting`/`playing`/`null`), effective frequency/gain/amp/loop, progress (`elapsed_sec`, `cycle_position_sec`, `cycle_number`), last error |
| `POST` | `/play/{signal_id}` | start transmission. Query parameters (all optional, override the values from `library.json` for this session only): `freq_hz`, `tx_vga_gain`, `amp_enable`, `loop` |
| `POST` | `/stop` | stop the current transmission |
| `WS` | `/ws/spectrum` | spectrum stream of the active broadcast (frames like `{"freqs": [...], "db": [...]}`) |
| `WS` | `/ws/preview/{signal_id}` | spectrum preview of a signal without transmitting to the HackRF |
| `POST` | `/import-local?path=...` | start importing a bundle from the server's disk (asynchronous, see section 9) |
| `GET` | `/import/status` | progress of the current import |
| `GET` | `/import/browse?path=...` | directory listing for the import file browser |

The phase (`phase`) in `/status` — `"starting"` appears right after the
transmit thread starts (while the device preflight check and the start of
`hackrf_transfer` are in progress, which can take a couple of seconds), then
changes to `"playing"`. The frontend waits specifically for `null` to
consider the transmission stopped — in the intermediate phases the "Stop"
button stays active.

---

## Installation and running

```bash
pip install -r requirements.txt
```

`hackrf_transfer` (part of the `hackrf` / PothosSDR package) must be
available in `PATH`, or specify the path explicitly with an environment
variable:

- Windows (PothosSDR): `set HACKRF_TRANSFER_BIN=D:\Program Files\PothosSDR\bin\hackrf_transfer.exe`
- Linux / Raspberry Pi: `sudo apt install hackrf` — usually ends up in `PATH` right away.

If `hackrf_transfer` (the executable itself) is not found, the backend
automatically switches to simulation mode (see section 5, and the important
caveat there: this is not the same as "the device is not connected" — if the
binary exists but there is no connection, you get a real error, not
simulation). The interface works fully, just without real transmission.

Run:
```bash
cd backend
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```
Open `http://localhost:8000`.

Optionally — restrict where bundles can be imported from (see section 9):
```bash
set IMPORT_ALLOWED_ROOTS=D:\usb-signals
```

### HackRF sample rate

Different builds of `hackrf_transfer` support different ranges: older ones
(including the one shipped with PothosSDR) sometimes support only a fixed set
of 8/10/12.5/16/20 MHz; newer ones support arbitrary values in the range
2–20 MHz. **2 MHz has been verified manually and works stably** — it is the
default of the converters. A wider bandwidth (for signals wider than a few
kHz) will require a higher rate — but file size and conversion time grow
proportionally.

---

## Adding and annotating signals

There are two tools for adding signals; both create the file, register the
signal and build the spectrum cache at once — nothing else needs to be done:

- **From a WAV recording of an HF signal** — `tools/wav_to_iq_library.py`
  (see the [pipeline details](#2-wav-to-iq-conversion-wav_to_iq_librarypy)).
- **Pure tone/carrier for calibration** — `tools/generate_sine.py` (see the
  [details](#3-test-tone-generation-generate_sinepy)).

The third tool — **editing the technical data sheet of an already added
signal** without reconverting: `tools/annotate_signal.py` (see
[section 8](#8-signal-technical-spec-signal_specpy)).

After running any of them, the signal immediately appears in the
interface's drop-down list — no server restart is needed (the signal list is
re-read from `library.json` on every request to `/signals`).

---

## Exchanging signals between machines

Export (on the source machine):
```bash
cd tools
python3 export_bundle.py --ids stanag-4285 test-tone-1khz --output bundle.tar --library ../library
# or the whole library at once:
python3 export_bundle.py --all --output full_export.tar --library ../library
```

Import (on the receiving machine) — only through the interface, the "+
Import" button: it opens a file browser over the server's allowed
directories (see [section 9](#9-bundle-export-and-import) for how to set
what exactly is allowed). Copy the `.tar` to a USB stick or an allowed
network folder, plug it into / connect it to the machine running the
backend, and pick the file in the browser — everything after that is
asynchronous, with a progress bar. If the recipient's library already had a
signal with the same `id`, it will be replaced (the import result explicitly
marks this as "updated").

Uploading a bundle through the browser itself (`multipart/form-data`) is no
longer supported — see [section 9](#9-bundle-export-and-import) for why it
was dropped.

---

## Web interface

Left panel (440 px):
- **Signal drop-down list** + a **"Preview"** button next to it — opens a
  modal window with the spectrum plot of the selected signal, played in a
  loop, without transmitting on air. Closed by the cross, by clicking the
  backdrop, or with the Escape key.
- **Details card** — recommended frequency, duration, sample rate, sideband,
  file amplitude and (if filled in) the **technical data sheet** —
  modulation, rate, FEC, etc. as a separate block; rows without data are
  simply not shown.
- **Transmit frequency** — an editable field, pre-filled with the signal's
  recommended frequency.
- **TX VGA gain** — an editable field, 0–47 dB.
- **Amplifier** and **Loop** — toggles.
- **Progress bar** — appears only during transmission (`phase ==
  "playing"`): filled according to the position within the current pass,
  with labels like `0:15 / 0:27` on the left and "Cycle N" on the right (for
  looped signals). Blue while based on time — until no confirmed pass has
  happened; after that — based on fact, see
  [section 7](#7-playback-progress).
- **Start/Stop** — at the bottom of the panel. During transmission the whole
  settings block (list, frequency, gain, toggles) is visually locked (dimmed);
  the "Preview" button stays active — it does not interfere with the
  ongoing transmission in any way.

Right panel — the spectrum (a line, with axes and labels) and the waterfall
(a scrolling spectrogram, same frequency scale, same left margin — they
match visually). Both use a monochrome green palette with a slight glow, in
keeping with the terminal style of the interface.

Header — the title and the **"+ Import"** button, which opens a file browser
over the server's disk (see section 9).

---

## Porting to Raspberry Pi 4 (8 GB) with a touchscreen

1. Copy the project to the Pi, `pip install -r requirements.txt`,
   `sudo apt install hackrf`.
2. The interface is already adapted for touch: large buttons, no hover
   effects (`:hover`), `touchstart` handling.
3. Backend autostart — a systemd unit:
   ```ini
   # /etc/systemd/system/iq-broadcast.service
   [Unit]
   Description=IQ Broadcast backend
   After=network.target

   [Service]
   WorkingDirectory=/home/pi/iq_broadcast/backend
   ExecStart=/usr/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 8000
   Restart=on-failure
   User=pi

   [Install]
   WantedBy=multi-user.target
   ```
   ```bash
   sudo systemctl enable --now iq-broadcast
   ```
4. Browser autostart in kiosk mode (depends on the Raspberry Pi OS image —
   autologin + autostart for LXDE/Wayfire):
   ```bash
   chromium-browser --kiosk --noerrdialogs --disable-infobars http://localhost:8000
   ```
5. USB permissions for the HackRF without root: a udev rule
   (`/etc/udev/rules.d/53-hackrf.rules`, shipped with the `hackrf` package)
   and adding the `pi` user to the `plugdev` group.
6. Importing bundles from a USB stick — on Linux/Pi this is `/media/...` or
   `/mnt/...`, picked up automatically by the `IMPORT_ALLOWED_ROOTS`
   defaults (see section 9); setting the variable manually is not required.
7. On Linux the whole graceful-stop logic (`SIGINT`) is much simpler than on
   Windows — no fiddling with separate consoles is required; that is a purely
   Windows-specific part of `hackrf_tx.py` (the code for it is guarded by an
   `IS_WINDOWS` check and simply does not execute on Linux).

---

## Debugging history and known pitfalls

In brief — what was tried and why it was dropped; worth keeping in mind for
further development:

- **Streaming samples into hackrf_transfer via stdin** (`-t -`) — the first
  transmission variant; it broke off after ~1 second on Windows with the
  error `streaming terminated (-1004)`. The cause was insufficiently fast and
  stable data delivery through Python in real time. Reading the very same
  file from disk (`-t <file>`) proved stable for any length of time — that is
  where we settled.
- **Rendering to the HackRF rate on the fly on every play** (from a compact
  master file) — worked, but added a noticeable delay before every
  transmission start. Replaced by direct conversion to the target format
  once, when a signal is added.
- **`terminate()` to stop hackrf_transfer on Windows** — a hard
  `TerminateProcess()`, gives the process no chance to release the USB
  device properly; the HackRF stayed in the "transmitting" state until
  reconnected. Replaced by an honest `CTRL_C_EVENT` via a separate helper
  process.
- **`CTRL_BREAK_EVENT` instead of `CTRL_C_EVENT`** — the first attempt at a
  graceful stop, did not work: this build of `hackrf_transfer` listens
  specifically for `CTRL_C_EVENT` (code `0`); `CTRL_BREAK_EVENT` (code `1`)
  is not recognized by its handler.
- **`AttachConsole`/`FreeConsole` directly in a thread of the backend
  process** — caused a complete server hang: these calls act at the level of
  the whole process, not a thread, and for a short time left **the entire**
  `uvicorn` without a console, which made logging in other threads hang. The
  solution was to move all this fiddling into a separate short-lived helper
  process.
- **A "race" on fast stop → play** — right after the previous transmission
  was stopped, the HackRF sometimes did not manage to release, and the next
  start hit `HackRF not found (-5)`. The solution is `_preflight_check` with
  several attempts and a small delay between them.
- **Monitoring stdout instead of stderr for hang detection** — the first
  version of the "alive but hung" detector listened to the stdout of
  `hackrf_transfer`, and so it fired falsely almost right after start. The
  cause: all diagnostic output (including periodic status lines) actually
  goes to **stderr**; nothing is written to stdout with `-t <file>`. Fixed by
  switching to stderr.
- **Linear interpolation during resampling creates images** — at a
  resampling ratio of hundreds, plain linear interpolation left a spurious
  signal at the frequency `-(fs_source - f_signal)`, 20–25 dB below the peak.
  Fixed by adding a low-pass filter after resampling (Butterworth order 8 via
  the SOS representation — the ordinary `(b, a)` for such a high order and
  low normalized cutoff frequency turned out to be numerically unstable and
  produced `NaN`).
- **Path traversal protection broke on Windows** — the safe-path check when
  unpacking bundles compared strings with a hard-coded Unix separator `/`,
  while on Windows `Path.resolve()` returns paths with `\` — the check failed
  for absolutely ANY file in the archive, not only malicious ones. Fixed by
  switching to `Path.relative_to()`, which works with `Path` objects rather
  than strings and is equally correct on any OS.
- **System temp on a different drive than the library** — unpacking a bundle
  via `tempfile.TemporaryDirectory()` uses the system temp by default (almost
  always drive `C:` on Windows), so unpacking a large bundle failed with `No
  space left on device` even when the drive with the library itself had
  plenty of space. Fixed — the temporary folder is now created inside
  `library/`, on the same drive the files are ultimately copied to.
- **Detecting drives on Windows via `Path("D:\\").exists()`** — sometimes
  tries to query the drive itself and can fail with an error on an empty
  CD drive/card reader with no media inserted, taking down the entire drive
  list at once because of one problematic letter. Fixed by switching to the
  WinAPI `GetLogicalDrives()` — reads the table of registered letters from
  the OS without touching the devices themselves.

---

## TX level safety

> ⚠️ **Warning.** This software transmits real RF signals. Make sure you are
> legally permitted to transmit on the chosen frequency and at the chosen
> power in your jurisdiction (licensing, band plans, emission limits).
> Prefer a dummy load or a shielded/attenuated test setup over a radiating
> antenna. You are solely responsible for how you use this software.

Start with small `tx_vga_gain` values (for example 10–15) and with
`amp_enable` off, raise them gradually, monitoring the real power at the
output of the antenna path. The default values in the library are
approximate and are not calculated for a specific antenna/amplifier/legally
permitted power on your frequency.

---

## Appendix: full signal list

91 signals at the time of writing. A dash (`—`) means the field is not filled
in (no reliable source, see section 8). Frequencies are the recommended
defaults and can always be overridden in the interface before starting.

| ID | Name | Frequency, MHz | Modulation | Tones | Baud | Shift, Hz | Bandwidth, Hz | Bitrate, bit/s | FEC |
|---|---|---|---|---|---|---|---|---|---|
| **STANAG/MIL** | | | | | | | | | |
| `ale-400` | MIL/NATO ALE | 11 | FSK (8-ary) | 8 | 125 | 250 | — | 375 | Golay (24,12) |
| `mil-188-110-16tone` | MIL-STD-188-110A App.B (16-tone) | 9.4 | DPSK (16 parallel tones) | 16 | — | — | — | — | — |
| `mil-188-110-39tone` | MIL-STD-188-110A App.B (39-tone) | 9.8 | DPSK (39 parallel tones) | 39 | — | — | — | — | — |
| `mil-188-110a` | MIL-STD-188-110A | 9 | PSK (2..8-PSK) | — | 2400 | — | 3000 | — | Convolutional (Viterbi) |
| `mil-188-110b` | MIL-STD-188-110B | 9 | PSK (2..8-PSK) | — | 2400 | — | 3000 | — | Convolutional (Viterbi), optional Reed-Solomon |
| `mil-188-141a` | MIL-STD-188-141A (ALE) | 13 | FSK (8-ary) | 8 | 125 | 250 | — | 375 | Golay (24,12) |
| `mil-188-141b` | MIL-STD-188-141B (ALE) | 13.4 | FSK (8-ary) | 8 | 125 | 250 | — | 375 | Golay (24,12) |
| `mil-m-55529a` | MIL-M-55529A | 16.3 | FSK | — | — | — | — | — | — |
| `stanag-4285` | STANAG-4285 | 5 | PSK (BPSK/QPSK/8PSK, depends on rate) | — | 2400 | — | 3000 | — | Convolutional (Viterbi) |
| `stanag-4415` | STANAG-4415 | 6.2 | — | — | — | — | — | 75 | Concatenated error-correction coding (for operation at very low SNR) |
| `stanag-4481-fsk` | STANAG-4481 (FSK) | 4.5 | FSK (synchronous) | — | 75 | 850 | — | — | — |
| `stanag-4481-psk` | STANAG-4481 (PSK) | 10.5 | BPSK (single 1800 Hz subcarrier) | — | 2400 | — | — | 300 | Convolutional, code rate 1/4 |
| `stanag-4529` | STANAG-4529 | 12 | PSK (BPSK/QPSK/8PSK, depends on rate) | — | 1200 | — | 1240 | — | Convolutional (same as STANAG 4285/4539) |
| **Aviation/utility** | | | | | | | | | |
| `chu` | CHU (time signal) | 7.85 | AM (voice) + BCD time code on a 1000 Hz subcarrier | — | — | — | 3000 | — | none |
| `dsc-hf` | DSC (GMDSS, HF) | 8.415 | FSK (2 tones) | — | 100 | 170 | — | — | Error detection + symbol repetition |
| `hf-acars` | HF-ACARS | 8.834 | MSK | — | 300 | — | — | 300 | — |
| `icao-selcal` | ICAO SELCAL | 10.1 | Two-tone (pairs of simultaneous tones) | 16 | 1 | — | — | — | none |
| **MFSK** | | | | | | | | | |
| `alis-2` | ALIS-2 | 12.5 | MFSK | — | — | — | — | — | — |
| `aum-13` | AUM-13 | 12.9 | MFSK | — | — | — | — | — | — |
| `cis-36-mfsk` | CIS-36 (MFSK) | 12.1 | MFSK | — | — | — | — | — | — |
| `coquelet-13` | Coquelet-13 | 13.7 | FSK (Coquelet, multichannel) | — | — | — | — | — | — |
| `coquelet-8` | Coquelet-8 | 13.3 | FSK (Coquelet, multichannel) | — | — | — | — | — | — |
| `coquelet-80` | Coquelet-80 | 14.5 | FSK (Coquelet, multichannel) | — | — | — | — | — | — |
| `mfsk-16` | MFSK-16 | 10.9 | MFSK | 16 | 15.625 | 15.625 | 316 | — | Convolutional (R=1/2, K=7, NASA) |
| `mfsk-20` | MFSK-20 | 11.3 | MFSK | — | — | — | — | — | — |
| `mfsk-8` | MFSK-8 | 10.5 | MFSK | 8 | 7.8125 | 7.8125 | — | — | Convolutional (R=1/2, K=7, NASA) |
| `olivia` | Olivia MFSK | 7.073 | MFSK | 32 | 31.25 | 31.25 | 1000 | — | Built-in redundant coding (resilient to deep fading) |
| `piccolo-mk6` | Piccolo MK6 | 15.3 | MFSK (Piccolo) | — | — | — | — | — | — |
| `sp-14` | SP-14 | 11.7 | MFSK | 13 | — | — | — | — | — |
| `twinplex` | Twinplex | 15.7 | FSK (twin channel) | — | — | — | — | — | — |
| **PSK** | | | | | | | | | |
| `alfrds` | ALFRDS | 21.7 | PSK | — | — | — | — | — | — |
| `cis-12` | CIS-12 | 20.5 | PSK | — | — | — | — | — | — |
| `clover-2` | CLOVER-II | 18.9 | Adaptive PSK/QAM (CLOVER) | — | — | — | — | — | — |
| `clover-2000` | CLOVER-2000 | 19.3 | Adaptive PSK/QAM (CLOVER) | — | — | — | — | — | — |
| `codan-9001` | CODAN 9001 Selcall | 19.7 | Tone selective calling (CODAN) | — | — | — | — | — | — |
| `gw-psk` | Globe Wireless PSK | 20.1 | PSK (Globe Wireless HF Network) | — | — | — | — | — | — |
| `pactor-ii` | PACTOR-II | 20.9 | DPSK (adaptive, 2/4-DPSK) | — | — | — | — | — | Memory ARQ + Huffman compression |
| `pactor-ii-fec` | PACTOR-II FEC | 21.3 | DPSK (adaptive, 2/4-DPSK) | — | — | — | — | — | FEC (broadcast mode) |
| `pactor-iii` | PACTOR-III | 14.1 | DPSK/16-DPSK (adaptive) | — | — | — | 2200 | — | Convolutional + memory ARQ |
| `psk-10` | PSK-10 | 16.9 | BPSK | — | 10 | — | — | — | none |
| `psk-125f` | PSK-125F | 17.7 | BPSK/QPSK (with FEC) | — | 125 | — | — | — | Convolutional (R=1/2, K=5) |
| `psk-220f` | PSK-220F | 18.1 | BPSK/QPSK (with FEC) | — | 220 | — | — | — | Convolutional (similar to PSK63F/125F) |
| `psk-31` | PSK-31 | 16.1 | BPSK | — | 31.25 | — | 80 | — | none |
| `psk-31-fec` | PSK-31 FEC | 16.5 | BPSK | — | 31.25 | — | — | — | Bit repetition over 13 positions (time diversity) |
| `psk-63f` | PSK-63F | 17.3 | BPSK/QPSK (with FEC) | — | 62.5 | — | — | — | Convolutional (R=1/2, K=5) |
| `psk-am` | PSK-AM | 18.5 | PSK+AM (hybrid) | — | — | — | — | — | — |
| **FSK/ARQ** | | | | | | | | | |
| `alis` | ALIS | 3.4 | FSK | — | — | — | — | — | — |
| `arq-e` | ARQ-E | 3.8 | FSK | — | — | 170 | — | — | — |
| `arq-e3` | ARQ-E3 | 4.2 | FSK | — | — | — | — | — | — |
| `arq-m2-242` | ARQ-M2-242 | 4.6 | FSK (2-channel multiplex) | — | — | — | — | — | — |
| `arq-m2-342` | ARQ-M2-342 | 5.4 | FSK (2-channel multiplex) | — | — | — | — | — | — |
| `arq-m4-242` | ARQ-M4-242 | 5.8 | FSK (4-channel multiplex) | — | — | — | — | — | — |
| `arq-m4-342` | ARQ-M4-342 | 6.6 | FSK (4-channel multiplex) | — | — | — | — | — | — |
| `arq-n` | ARQ-N | 7 | FSK | — | — | — | — | — | — |
| `arq6-90` | ARQ6-90 | 7.4 | FSK | — | — | — | — | — | — |
| `arq6-98` | ARQ6-98 | 7.8 | FSK | — | — | — | — | — | — |
| `ascii-br6028` | ASCII BR-6028 | 9.6 | FSK | — | — | — | — | — | — |
| `ascii-fsk` | ASCII (FSK) | 9.2 | FSK | — | — | — | — | — | — |
| `autospec` | Autospec | 10.3 | FSK | — | — | — | — | — | — |
| `baudot` | Baudot (RTTY) | 10.7 | FSK | — | — | — | — | — | none |
| `bulg-ascii` | Bulgarian ASCII | 11.1 | FSK | — | — | — | — | — | — |
| `cis-11` | CIS-11 | 11.5 | FSK | — | — | — | — | — | — |
| `cis-14` | CIS-14 | 11.9 | FSK | — | — | — | — | — | — |
| `cis-36-50` | CIS-36/50 | 12.3 | FSK | — | — | — | — | — | — |
| `cis-50-50` | CIS-50/50 | 12.7 | FSK | — | — | — | — | — | — |
| `dup-arq` | DUP-ARQ | 13.1 | FSK (duplex ARQ) | — | — | — | — | — | — |
| `dup-arq-2` | DUP-ARQ-2 | 13.5 | FSK (duplex ARQ) | — | — | — | — | — | — |
| `dup-fec-2` | DUP-FEC-2 | 13.9 | FSK (duplex FEC) | — | — | — | — | — | — |
| `fec-a` | FEC-A | 14.3 | FSK | — | — | — | — | — | — |
| `g-tor` | G-TOR | 14.7 | Adaptive (FSK/GMSK) | — | — | — | — | — | Reed-Solomon + Huffman compression |
| `gw-fsk` | Globe Wireless FSK | 15.1 | FSK (Globe Wireless HF Network) | — | — | — | — | — | — |
| `hc-arq` | HC-ARQ | 15.5 | FSK (ARQ) | — | — | — | — | — | — |
| `hng-fec` | Hungarian FEC | 15.9 | FSK | — | — | — | — | — | — |
| `packet-300` | AX.25 Packet 300 | 16.7 | AFSK (Bell 103) | — | 300 | 200 | — | 300 | none (CRC for error detection) |
| `pactor` | PACTOR-I | 17.1 | FSK (2 tones) | — | 100 | 200 | — | — | Memory ARQ + Huffman compression |
| `pactor-fec` | PACTOR FEC | 17.5 | FSK (2 tones) | — | 100 | 200 | — | — | FEC (broadcast mode) |
| `pol-arq` | POL-ARQ | 17.9 | FSK | — | — | — | — | — | — |
| `rum-fec` | Romanian FEC | 18.3 | FSK | — | — | — | — | — | — |
| `si-arq` | SI-ARQ | 18.7 | FSK | — | — | — | — | — | — |
| `si-fec` | SI-FEC | 19.1 | FSK | — | — | — | — | — | — |
| `sitor-arq` | SITOR-ARQ | 8.4 | FSK (2 tones) | 2 | 100 | 170 | 400 | — | Error detection (CCIR-476) + ARQ retransmission |
| `sitor-fec` | SITOR-FEC | 3 | FSK (2 tones) | 2 | 100 | 170 | — | — | SITOR-B/FEC (each character sent twice) |
| `spread-51` | Spread-51 | 19.5 | FSK | — | — | — | — | — | — |
| `swed-arq` | SWED-ARQ | 19.9 | FSK | — | — | — | — | — | — |
| **Graphics/CW** | | | | | | | | | |
| `cw-morse` | CW / Morse | 20.7 | OOK (CW) | — | — | — | — | — | none |
| `feldhell` | Feld-Hell | 21.1 | OOK (Hellschreiber) | — | 122.5 | — | 75 | — | none |
| `fmhell` | FM-Hell | 21.5 | MSK (Hellschreiber variant) | — | — | — | — | — | none |
| `press-fax` | Press-Fax | 21.9 | FM (frequency-modulated fax) | — | — | — | — | — | none |
| `sstv` | SSTV | 20.3 | FM (analog) | — | — | — | — | — | none |
| `weatherfax` | Weatherfax | 22.3 | FM (frequency-modulated fax) | — | — | — | — | — | none |
| **Utility** | | | | | | | | | |
| `test-tone-1khz` | Test tone +1 kHz | 14 | Unmodulated tone | 1 | — | — | — | — | none |
