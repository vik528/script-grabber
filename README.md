# Script Grabber

Standalone multi-camera capture utility for Basler cameras (GigE/USB3) using
`pypylon` — the same devices Pylon Viewer already sees. Captures a chosen
number of images from one or more simultaneously-connected cameras. Camera
settings (exposure, gain, etc.) are left exactly as configured in Pylon
Viewer — the script never writes acquisition parameters in its default flow.

## Setup

Requires Python 3.9 or newer. `pip install pypylon` gets you a prebuilt
wheel on all three OSes below — no compiling anything.

**Close Pylon Viewer (or disconnect its cameras there) before running this
script**, on any OS. Basler cameras only allow one exclusive connection at a
time — if Viewer is still connected, this script will fail to open the
camera and will tell you that's the likely cause.

### Ubuntu / Linux

```bash
pip install pypylon
```

If Pylon Viewer is already installed and working, the USB3 udev rules and
GenTL producers this needs are almost certainly already in place.

### macOS

Requires macOS Sonoma (14.0) or newer (both Intel and Apple Silicon are
supported).

```bash
pip3 install pypylon
```

- Also installing Basler's **pylon Camera Software Suite** (the same
  installer Pylon Viewer comes from) isn't required by pypylon itself, but
  is recommended — it provides the GenTL producers/network stack GigE
  cameras need and gives you Viewer as a fallback for eyeballing settings.
- **GigE cameras on macOS** often don't auto-discover as easily as on
  Linux/Windows. If a camera shows as "Unreachable," use Basler's **pylon IP
  Configurator** (installed with the Software Suite) to manually set your
  Mac's Ethernet adapter to the same subnet as the camera.
- `--gui` mode needs a working Tkinter. Python installed from python.org
  includes it. If you're using Homebrew's `python3` and get
  `ModuleNotFoundError: No module named '_tkinter'`, run
  `brew install python-tk@3.x` (matching your Python's minor version).

### Windows

Requires Windows 10/11 64-bit.

```powershell
pip install pypylon
```

- Also installing Basler's **pylon Camera Software Suite** is recommended —
  it installs the GenTL producers/drivers GigE and USB3 cameras need, plus
  Pylon Viewer.
- **GigE cameras on Windows**: if a camera doesn't show up, check that
  Windows Defender Firewall (or any third-party firewall) isn't blocking
  GigE Vision discovery traffic — allow the pylon/GigE Vision app through it.
- Tkinter (`--gui` mode) ships by default with the standard python.org
  Windows installer — no extra step needed.

## Usage

Commands below use `python3` (Linux/macOS convention). On Windows, use
`python` or `py -3` instead.

Interactive (recommended first run — walks you through everything):

```bash
python3 capture_cameras.py
```

You'll be shown the detected cameras, then prompted to pick which ones to
use, how many images per camera, and the output format.

Non-interactive / scriptable:

```bash
python3 capture_cameras.py --cameras all --count 10 --format tiff --outdir ./captures
```

- `--cameras` — comma-separated indices (e.g. `0,2`) or `all`
- `--count` — images to capture per camera
- `--format` — `tiff` (default, lossless/full bit depth), `png`, or `bmp` (note: BMP always saves 8-bit)
- `--outdir` — output directory (default `./captures`); each camera gets its own subfolder inside it, named `<model>_<serial>`

Optional session GUI (camera selection + plate/bolts + capture):

```bash
python3 capture_cameras.py --gui
```

**GUI session capture (v1.3.0+):**
- **Plate Color** (Black | Silver, default Black) and **Bolts Size**
  (Big | Small, default Big) — single-answer radios, written into each
  shot's `capture_manifest.json`.
- **BMP only** in the GUI (read-only label). Use the CLI for TIFF/PNG.
- **Folder layout:** Browse picks the MAIN outdir. Each Capture click
  creates `<main>/<MM-DD>/<HHMMSS>/` from the local computer's date/time
  (e.g. `./captures/09-04/143052/`) and saves every selected camera into
  that one shot folder as `NorthCam_001.bmp`, `SouthCam_001.bmp`, …
  plus `capture_manifest.json`. Count `N` → `Alias_001.bmp` … `Alias_00N.bmp`.
- **Camera names by serial:** `40044823`→NorthCam, `40048976`→SouthCam,
  `40519358`→TopCam (shown on the camera card). Unknown serials fall back
  to `sanitize(model)_serial`. Capture is sequential (no GUI Group/PTP).
- Controls: Rescan, Browse, Capture, Count, Folder — no Apply Settings.

The GUI opens fine even if no cameras are detected yet (e.g. you launched
it before powering on/connecting a camera) — it shows "No cameras
detected" instead of erroring out. Click **Rescan** at any point to
re-detect cameras and rebuild the list without restarting the app (blocked
with a warning if a capture is currently in progress).

The navy header includes a **customer logo slot** on the right. Click the
"ADD LOGO" placeholder to choose a PNG, GIF, or PPM/PGM file (Tk's
built-in `PhotoImage` — no extra image library). The chosen path is saved
in `~/.config/script-grabber/ui.json` on Linux/macOS, or
`%APPDATA%\script-grabber\ui.json` on Windows, and reused on the next
launch. If that file is missing later, the placeholder returns. No logo
image is bundled with the app.

## Testing without hardware attached

Emulates virtual cameras (2, here) so you can exercise the full flow
(including `--gui`) before pointing it at real hardware. How you set the
environment variable depends on your shell:

```bash
# Linux / macOS (bash or zsh)
PYLON_CAMEMU=2 python3 capture_cameras.py
```

```cmd
:: Windows Command Prompt
set PYLON_CAMEMU=2 && python capture_cameras.py
```

```powershell
# Windows PowerShell
$env:PYLON_CAMEMU=2; python capture_cameras.py
```

## Filenames

**CLI:** images are saved as `<model>_<serial>_<shotindex>_<timestamp>.<ext>`
under `<outdir>/<model>_<serial>/`.

**GUI:** images are saved as `<Alias>_<shot:03d>.bmp` under
`<main_outdir>/<MM-DD>/<HHMMSS>/` (local date/time when Capture is clicked).

## Standalone executable (Linux)

For handing this tool to someone without a Python environment: `packaging/linux/`
builds a single-file Linux executable via Docker + PyInstaller, no local
Python/pip setup required on the machine that runs it.

```bash
docker build -f packaging/linux/Dockerfile -t script-grabber-build .
docker create --name sg-extract script-grabber-build
docker cp sg-extract:/build/dist/script-grabber ./script-grabber-linux
docker rm sg-extract
```

Notes:
- Builds against Ubuntu 24.04 (glibc 2.39) — the oldest Ubuntu this tool is
  deployed on. Targeting x86_64 explicitly (pinned in the Dockerfile,
  regardless of the build host's own architecture — matters if you're
  building from an Apple Silicon Mac). Uses stock Python 3.12 from the
  image (no compile-from-source step; that was only needed on 20.04's
  Python 3.8). The frozen binary needs Ubuntu 24.04+.
- **`pypylon`/`pyinstaller`/`pyinstaller-hooks-contrib` versions are pinned
  exactly** in the Dockerfile (as build ARGs, currently `26.7`/`6.22.2`/
  `2026.7`) — deliberately not left to "whatever's latest on PyPI today."
  This means running `docker build` on a different machine, or months from
  now, reproduces the same tested build rather than silently drifting onto
  newer package versions that might reintroduce a bug like the symlink
  issue below or expose a different API surface. Bump these deliberately
  (edit the `ARG` defaults or pass `--build-arg`), re-running the full
  smoke test + real-hardware checklist afterward — don't let them float.
- Uses a custom `script-grabber.spec` rather than plain `pyinstaller --onefile`
  flags — it works around a real, confirmed bug where PyInstaller's default
  bundling breaks pypylon's camera discovery entirely inside the frozen
  executable (see the `.spec` file's header comment for the full mechanism).
  Don't switch back to the plain CLI invocation without that fix.
- **Verified end-to-end, including on real hardware:** the emulator-based CLI
  path (`PYLON_CAMEMU`) works correctly from the frozen executable, `--version`
  prints correctly, and the GUI renders correctly (confirmed via an Xvfb
  screenshot with emulated cameras — both camera rows, the Group/Lens two-row
  layout, and all six bottom-bar controls including Rescan). **Real GigE
  camera discovery was confirmed working on an actual Ubuntu machine with real
  cameras attached** — copied the executable over with no Python/pip/venv on
  that machine at all, ran `--gui`, and it found and captured from a real
  camera with zero extra setup. GenTL discovery is genuinely self-contained in
  the frozen bundle, as hoped — no `GENICAM_GENTL64_PATH` workaround was
  needed. (Not yet separately re-confirmed on real hardware: the Group/Lens/
  Rescan features specifically — same code path as everything else already
  proven, so low-risk, but worth a pass before a real multi-camera
  data-collection session.)
- If you see a GigE buffer-underrun warning suggesting `pylonGigEConfigurator`
  during a real capture, that's a known network/bandwidth-tuning
  consideration (see the "Camera groups (hardware sync)" note below), not a
  packaging defect.

## Standalone executables (all platforms, via GitHub Actions)

`.github/workflows/build.yml` builds these on GitHub's hosted runners.
**Linux** builds on every push/PR to `main` (cheap — standard 1x-minute
runner). **macOS and Windows only build on a `v*` tag push or a manual
"Run workflow" dispatch** — this repo is private, and GitHub bills macOS
runner-minutes at 10x and Windows at 2x, so they're deliberately not
triggered by routine pushes:

- **Linux**: reuses `packaging/linux/Dockerfile` unchanged (same
  Docker-in-Docker build as the local instructions above) — same
  glibc-2.39 / Ubuntu 24.04 floor, same symlink-bug fix, same pinned versions.
- **macOS**: plain `pyinstaller --onefile`, no custom `.spec`. Confirmed by
  direct local testing (2026-08-28, Apple Silicon, Python 3.14) that
  PyInstaller performs an analogous top-level-symlink duplication here too
  (for `libNodeMapData`/`libMathParser` `.dylib`s specifically) — but unlike
  Linux, it does **not** break camera discovery; the `PYLON_CAMEMU` smoke
  test passes unmodified. Revisit with a custom `.spec` only if a future
  pypylon/PyInstaller version regresses this. Built as **two separate
  jobs/artifacts**, `script-grabber-macos-arm64` and `script-grabber-macos-intel`
  — pypylon ships separate arm64/x86_64 wheels with no universal2 build, and
  a standard Apple-Silicon runner can only ever produce an arm64 executable,
  so Intel needs its own runner (`macos-15-intel`) rather than a "fat" binary.
  Runner labels are pinned to `macos-15` explicitly rather than `macos-latest`,
  since that label was mid-migration to a newer macOS release as of mid-2026
  — a version-pinned build shouldn't sit on a runner OS that can change under it.
- **Windows**: plain `pyinstaller --onefile` — had no local machine to
  pre-validate against, so its first real test was this CI itself; the
  emulator smoke test passed on the first run, no custom `.spec` needed.

Every job runs the `PYLON_CAMEMU` emulator smoke test before publishing its
artifact — a build that doesn't actually find/capture from emulated cameras
fails the job rather than silently shipping. Push a tag matching `v*` (e.g.
`v1.2.0`) to also publish all three executables to a GitHub Release. **Real
GigE hardware discovery is only proven on Linux so far** (see above) — the
macOS/Windows CI builds are emulator-verified only; treat them the same way
Linux was treated before its own real-hardware pass.

**Downloading a Release asset**: GitHub Release assets don't preserve the
executable bit — after downloading, run `chmod +x script-grabber-linux` or
`chmod +x script-grabber-macos-arm64` (or `-intel`) before running it
(Windows' `.exe` doesn't need this).

**Unsigned executables**: none of these builds are code-signed or notarized.
On macOS, Gatekeeper only quarantines files downloaded via a browser/Mail/
Messages — a file pulled with `gh release download` or `curl` typically
isn't quarantined at all and just runs. If it *is* quarantined, the reliable
fix is `xattr -d com.apple.quarantine ./script-grabber-macos-arm64`; the
classic "right-click → Open" bypass **no longer works** as of macOS Sequoia
15.1 — if you don't want to use the command line, the only remaining GUI
path is System Settings → Privacy & Security → "Open Anyway" (appears only
for about an hour after a failed launch attempt, and needs a second confirm
click). On Windows, expect a SmartScreen "unknown publisher" warning — click
"More info" → "Run anyway". Neither is a bug in the build; it's the normal
experience for any unsigned executable from either OS vendor. Ad-hoc
code-signing/notarization is a future refinement, not a blocker for
internal use.

## Notes

- By default, cameras are captured **sequentially, one at a time** (camera
  2's images are taken right after camera 1 finishes, not at the same
  instant) — "simultaneously connected" here means multiple cameras attached
  to the machine at once, not hardware-synchronized/time-paired shots. The
  GUI session path (v1.3.0+) always captures this way into a shared dated
  shot folder.
- Exposure and other acquisition settings are never modified by the
  default flow — the script only captures with whatever is already
  configured on each camera (set in Pylon Viewer). The GUI no longer
  exposes Exposure / Apply Settings; a camera's existing Gain setting is
  left completely untouched.
- **GUI session fields:** Plate Color and Bolts Size are recorded in each
  shot folder's `capture_manifest.json` alongside per-camera file lists.
  See the Usage section above for folder layout and camera aliases.
- **Hardware-synchronized group capture (library / historical):** the
  module still contains IEEE 1588 PTP + GigE Vision Scheduled Action
  Command helpers (`capture_group_synchronized`), but they are **not**
  wired through the current GUI. They require GigE cameras with PTP
  support and cannot be exercised against `PYLON_CAMEMU`. See older
  CHANGELOG entries (1.0–1.1) for the original Group-field workflow.
- TIFF and PNG output preserve bit depth above 8-bit when the camera's
  current pixel format supports it (e.g. Mono12 → a genuine 16-bit file);
  BMP output is always 8-bit. A warning is logged if the chosen format
  can't hold the captured image without further conversion (confirmed: this
  fires for BMP with a >8-bit source, and does not fire for TIFF/PNG).
- Verified end-to-end against pypylon 26.7 with `PYLON_CAMEMU` emulated
  cameras: device enumeration/selection (both flag-based and the fully
  interactive prompts), invalid-input handling (bad index, zero/negative
  count, non-numeric input), the 8-bit and 16-bit converter paths,
  TIFF/PNG/BMP saving, and the exposure/gain node fallback all ran
  successfully and produced correct output. The `--gui` window was also
  driven end-to-end (camera checkboxes render correctly, clicking Capture
  runs the real background-thread capture path and produces valid files) —
  visual screenshot confirmation wasn't possible in the environment this was
  built in (no Screen Recording permission), but the underlying logic was
  exercised directly and worked. The exposure/gain fallback
  (`ExposureTime`/`Gain` vs. `ExposureTimeAbs`/`GainRaw`) was additionally
  confirmed against a real Basler GigE camera reachable from the network
  this was built on — that run also surfaced and fixed a real crash bug (an
  unreachable/flaky camera could kill the whole script or GUI; camera
  connection failures are now caught and reported per-camera instead). Not
  yet verified: behavior when Pylon Viewer is actively holding a camera
  open on **real** hardware (the software emulator doesn't enforce
  exclusive access, so this couldn't be triggered safely in testing) — the
  error handling for it is in place but unconfirmed against a real camera.
  Do a short real-hardware run with a small `--count` before relying on
  this for a full data-collection session.
- **Camera groups (hardware sync) — verified end-to-end against real
  hardware** (acA4112-8gc, a2A4200-12gcBAS, a2A4504-5gcBAS): PTP converged,
  the broadcast Action Command was accepted by real cameras and fired their
  `FrameStart` at the same scheduled instant, and a real, valid image was
  captured and saved. `TriggerMode`/`TriggerSource` correctly restored to
  their pre-capture values in every run, including ones that aborted
  partway through or timed out during PTP convergence. Real-hardware
  testing also caught and fixed several bugs the emulator/simulated-node
  tests could not have (wrong availability-check API, wrong node names for
  ace 2's PTP status/offset nodes, a wrong assumption about the
  timestamp-latch value being Unix-epoch-based when it's actually
  device-relative — see the plan doc's "Step 2 real-hardware findings" for
  detail). Known caveats found during this testing, not fixed in code
  (deliberately — they're network/hardware conditions, not this script's to
  silently override):
  - `acA4112-8gc` hit a GigE buffer-underrun error (`0xe1000014`) during
    synchronized capture in this session — a standard GigE Vision
    bandwidth/packet-size tuning issue (see Basler's `pylonGigEConfigurator`
    tool), not a sync failure; the Action Command still fired correctly.
    Hardware-synced capture creates a bandwidth spike (all cameras
    transmitting at once) that sequential capture avoids by staggering —
    worth tuning Inter-Packet Delay/packet size for your specific network
    before relying on multi-camera synchronized capture at full resolution.
  - `a2A4200-12gcBAS`'s PTP lock was intermittent/flaky in this session
    (sometimes converged quickly, sometimes timed out after the full 90s) —
    worth checking that camera's cable/switch port before relying on it in
    a group.
  - Ctrl-C during active PTP convergence polling didn't interrupt promptly
    in this session's (non-interactive, programmatic-signal) test harness —
    isolated to something in the repeated per-iteration GenICam network
    calls, not camera-open or PTP-enable themselves. `TriggerMode` was
    confirmed correctly restored on every exit path tested. Verify directly
    with a real interactive Ctrl-C before depending on instant
    interruptibility.
- **Reliability tuning for grouped capture (added after real-world testing
  showed a 3rd camera in a group aggravating the buffer-underrun issue
  above):** grouped/synchronized cameras now get `MaxNumBuffer` raised
  (default is 10; raised to 16+ for headroom during a synchronized burst)
  and `AutoPacketSize` enabled (negotiates the largest safe packet size
  automatically, rather than guessing a jumbo-frame size that might exceed
  what your NIC/switch path actually supports) — both applied only to
  grouped cameras, before grabbing starts, and neither needs restoring
  afterward (both are host-side session settings, not written to the
  camera). A non-blocking bandwidth check also runs once per group capture:
  it sums each camera's `GevSCBWA` (actual bandwidth demand at current
  settings) and logs a warning if the combined total looks tight against
  one GigE link's ~125MB/s budget — a concrete signal for "this group won't
  fit on one shared link," not just a guess. The scheduling margin between
  shots is now adaptive too: starts at the same safe 500ms, but shrinks
  (with a 100ms floor) after a few consecutive clean rounds, and instantly
  grows back on any `_ActionLate` — cuts per-shot overhead on longer runs
  without sacrificing the safety margin when the network needs it.
  **`AutoPacketSize`/`GevSCBWA` are GigE-transport-specific and cannot be
  exercised against the `PYLON_CAMEMU` emulator at all** (confirmed: neither
  node exists on the emulator's node maps) — verified via unit tests with
  simulated nodes that the surrounding logic (defensive probing, threshold
  math, buffer-count comparison) is correct.
  **Real-hardware status (2026-08-28):** 2-camera grouped/synchronized
  capture with v1.1.0 confirmed working well on real hardware. 3-camera
  grouped capture — the original motivating case — has **not** been
  re-confirmed since these changes; testing is deferred for now. Accepted
  as a reasonable risk for production specifically because typical
  production hardware uses a 2.5Gb host NIC/PoE switch against 1Gb cameras,
  giving headroom the dev/test setup's plain 1Gb uplink didn't have (that
  1Gb link is what originally hit the buffer-underrun/bandwidth-contention
  issue with 3 cameras). If you deploy on a plain 1Gb host NIC with 3+
  grouped cameras, re-verify this before trusting it, and watch for
  `AutoPacketSize not available` or a bandwidth warning in the log output.
- **`setup_ubuntu_gige.sh`** (repo root): OS-level complement to the above —
  tunes Ubuntu's network stack for multi-camera GigE bandwidth (jumbo-frame
  MTU, socket buffer size, NIC ring buffer, interrupt coalescing, real-time
  thread priority). Two modes: `./setup_ubuntu_gige.sh auto` runs Basler's
  own `PylonGigEConfigurator auto-all` if the pylon Camera Software Suite is
  installed; `./setup_ubuntu_gige.sh manual --iface <your-NIC>` applies the
  same class of tuning directly without needing that install. Always prints
  the full plan and asks for confirmation before changing anything
  (`--dry-run` to preview with zero changes, `--yes` to skip the prompt).
  Idempotent — safe to re-run. Only needed for grouped/synchronized capture;
  a single camera or sequential capture never needs any of this.
