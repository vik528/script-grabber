# Changelog

Manual version tracking — this is a single-engineer lab tool, no auto-updater.
Grab the new `script-grabber-linux` (or platform equivalent) file when told there's a new version.

## 1.3.0 — 2026-09-04

GUI session capture — plate/bolts fields, BMP-only GUI, dated shot folders,
and fixed camera aliases. CLI capture path is unchanged (still per-camera
folders; still tiff/png/bmp).

- **Session fields:** Plate Color (Black | Silver, default Black) and Bolts
  Size (Big | Small, default Big) as single-answer radio buttons; recorded
  in each shot's `capture_manifest.json`.
- **BMP-only GUI:** format dropdown removed; read-only BMP label. TIFF/PNG
  remain available from the CLI only.
- **Camera cards simplified:** include checkbox + model name + serial
  (muted). Exposure, Group, Lens (mm), Brand, and Model rows removed.
  Known serials show an alias label/badge (NorthCam / SouthCam / TopCam).
- **Dated shot folders:** Browse picks the MAIN outdir. Each Capture click
  writes into `<main>/<MM-DD>/<HHMMSS>/` (local date/time), with all
  selected cameras in that one shot folder as `Alias_001.bmp` …
  `Alias_00N.bmp`, plus one `capture_manifest.json` object
  (plate_color, bolts_size, timestamp, shot_folder, cameras, count, format).
- **Aliases by serial:** `40044823`→NorthCam, `40048976`→SouthCam,
  `40519358`→TopCam. Unknown serials use `sanitize(model)_serial` as the
  filename stem and still save into the shared shot folder.
- **No GUI Group/PTP path:** Capture is sequential into the shared shot
  folder. Apply Settings removed. Group/Lens help replaced with a short
  plate/bolts + folder-layout hint. Hardware-sync helpers remain in the
  module for CLI/library use but are not wired through the GUI.

## 1.2.0 — 2026-09-02

GUI visual polish only — capture, grouping, PTP, lens-manifest, rescan,
apply-settings, threading, and CLI behavior are unchanged.

- Dori-themed Tkinter/ttk shell: page background `#EAEEF0`, navy header
  (`#224C5C`) with product name + version, teal primary Capture button
  (`#17B696`), navy secondary actions (Rescan / Apply Settings / Browse).
- Customer logo slot in the header (~40px rounded well). Click the
  "ADD LOGO" placeholder to choose a PNG/GIF/PPM image (stdlib `PhotoImage`;
  no Pillow). Path is persisted in `~/.config/script-grabber/ui.json`
  (Linux/macOS) or `%APPDATA%\script-grabber\ui.json` (Windows). Missing
  file falls back to the placeholder. No logo is bundled.
- Visual refinement: rounded chrome on cards, the logo well, and the
  control bar (Canvas round-rect + inner Frame, 12–16px radius, soft 2px
  `#C5CFD4` shadow). Capture is a solid teal pill; Rescan / Apply Settings /
  Browse are navy-outline pills. Help is a teal text link. Header is one
  editorial line with a muted subtitle; empty state sits in a large padded
  card; status footer is quieter. Inputs remain ttk.
- Camera list restyled as white cards (model + serial, include checkbox,
  Exposure/Group, quieter lens row). Centered empty state when none are
  detected. Group/Lens help moved into a short hint + collapsible Help
  panel (same text, no longer always-visible stacked labels).
- Bottom action bar shows the output folder path. Status line uses
  annotation colors (ok `#00695C` / warning `#FFAB40` / error `#B71C1C`) by
  parsing existing status strings.

## 1.1.0 — 2026-08-28

Project moved into git, hosted at github.com/vik528/script-grabber. Added
`.github/workflows/build.yml`: CI builds and emulator-smoke-tests a Linux
executable on every push/PR to `main`, and Linux+macOS(arm64+Intel)+Windows
on a `v*` tag push or manual dispatch — publishing all four executables to
a GitHub Release on the tag push. Linux reuses the existing Docker build
unchanged; macOS builds separately for arm64 and Intel (pypylon ships no
universal2 wheel) as a plain PyInstaller onefile build (confirmed both
locally and in CI that the Linux symlink bug does not reproduce there);
Windows is also a plain onefile build, confirmed working on its first CI
run with no prior local validation possible (no Windows machine available).

Reliability tuning for grouped (synchronized) capture, added after real
3-camera testing hit GigE bandwidth issues:
- `MaxNumBuffer` raised and `AutoPacketSize` enabled on grouped cameras only
  (host-side session settings, never written to the camera).
- Non-blocking bandwidth check (`GevSCBWA` vs. ~125MB/s link budget) warns
  in the log if a group's combined demand looks too tight for one shared
  link.
- Adaptive scheduling margin — shrinks after clean rounds, grows instantly
  on `_ActionLate`, cutting per-shot overhead on longer runs.
- New `setup_ubuntu_gige.sh`: OS-level network tuning (jumbo-frame MTU,
  socket buffers, NIC ring buffer, interrupt coalescing, rtprio) for the
  same bandwidth-contention problem, complementing the code-level changes.
- **Note**: `AutoPacketSize`/`GevSCBWA` are GigE-specific and untestable
  against the emulator — verified via unit tests with simulated nodes, but
  real-hardware confirmation (does it actually take effect / help) is
  still pending.

## 1.0.0 — 2026-08-27

First versioned build. Everything up to and including:
- CLI + Tkinter GUI capture from Basler GigE/USB3 cameras via pypylon.
- GUI camera grouping for IEEE 1588 PTP + GigE Vision Scheduled Action Command
  hardware-synchronized capture, with grouped cameras saving into one shared
  output folder.
- Per-camera Lens (mm)/Brand/Model fields, recorded in a `capture_manifest.json`
  alongside the images.
- GUI opens with zero cameras connected; Rescan button re-detects without
  restarting.
- Standalone Linux executable via Docker + PyInstaller (`packaging/linux/`) —
  verified with emulated cameras; real-hardware/GUI verification on an actual
  Ubuntu machine still pending (see README's "Standalone executable" section).
