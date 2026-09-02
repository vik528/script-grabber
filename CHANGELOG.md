# Changelog

Manual version tracking — this is a single-engineer lab tool, no auto-updater.
Grab the new `script-grabber-linux` (or platform equivalent) file when told there's a new version.

## 1.2.0 — 2026-09-01

GUI visual polish only — capture, grouping, PTP, lens-manifest, rescan,
apply-settings, threading, and CLI behavior are unchanged.

- Dori-themed Tkinter/ttk shell: page background `#EAEEF0`, navy header
  (`#224C5C`) with product name + version, teal primary Capture button
  (`#17B696`), navy secondary actions (Rescan / Apply Settings / Browse).
- Customer logo slot in the header (~44px). Click the placeholder to choose
  a PNG/GIF/PPM image (stdlib `PhotoImage`; no Pillow). Path is persisted in
  `~/.config/script-grabber/ui.json` (Linux/macOS) or
  `%APPDATA%\script-grabber\ui.json` (Windows). Missing file falls back to
  the placeholder. No logo is bundled.
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
