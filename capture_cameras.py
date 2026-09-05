#!/usr/bin/env python3
"""
capture_cameras.py — Multi-camera capture utility for Basler cameras
(GigE / USB3, via pypylon / the Pylon SDK).

Auto-detects every Basler camera pypylon can see, lets you pick which ones to
use, and captures a chosen number of images from each. Camera settings
(exposure, gain, etc.) are left exactly as configured in Pylon Viewer — this
script never writes acquisition parameters at all. Run with --gui for an
optional session-oriented window (plate/bolts fields, BMP-only, dated shot
folders, fixed camera aliases — see README).

Exit codes: 0 = every selected camera saved every requested image; 1 = a
camera failed to open, errored mid-capture, or saved fewer images than
requested (also: no cameras found, bad arguments, pypylon missing);
130 = interrupted.

Usage:
    python3 capture_cameras.py
    python3 capture_cameras.py --cameras all --count 10 --format tiff --outdir ./captures
    python3 capture_cameras.py --gui

Testing without hardware: set PYLON_CAMEMU=2 (or more) before running to
have pypylon emulate that many virtual cameras.
"""
from __future__ import annotations

__version__ = "1.3.3"

import argparse
import datetime
import json
import logging
import os
import queue
import re
import sys
import threading
from typing import NamedTuple, Optional

try:
    from pypylon import pylon
except ImportError:
    sys.stderr.write(
        "ERROR: pypylon is not installed, or the Pylon SDK runtime could not "
        "be loaded.\n"
        "  Install with:  pip install pypylon\n"
        "  If that succeeds but this still fails, verify Pylon Viewer / the "
        "Pylon SDK is installed — pypylon's wheel bundles the runtime, but "
        "GenTL producers for some transport layers may need the full SDK.\n"
    )
    sys.exit(1)

LOG = logging.getLogger("capture_cameras")
RETRIEVE_TIMEOUT_MS = 5000

# Bounded retry budget for failed/incomplete buffers. A GigE buffer underrun
# or dropped packet costs an attempt but not a saved frame, so allow some
# headroom over `count` — but stay bounded: sustained packet loss must end
# the run with a reported shortfall, not spin forever.
GRAB_ATTEMPT_MULTIPLIER = 2


class CaptureResult(NamedTuple):
    """What one camera actually produced, against what was asked of it.

    The whole point of this type is that `saved` alone is ambiguous — a list
    of 3 filenames looks identical whether 3 or 10 images were requested.
    Callers decide with `.ok`, and report with `.summary()`.
    """

    saved: list
    requested: int
    error: Optional[str] = None
    serial: str = ""

    @property
    def shortfall(self) -> int:
        return max(0, self.requested - len(self.saved))

    @property
    def ok(self) -> bool:
        return self.error is None and self.shortfall == 0

    def summary(self) -> str:
        head = f"{self.serial}: {len(self.saved)}/{self.requested} saved"
        return head if self.error is None else f"{head} — {self.error}"

FILE_FORMATS = {
    "tiff": pylon.ImageFileFormat_Tiff,
    "png": pylon.ImageFileFormat_Png,
    "bmp": pylon.ImageFileFormat_Bmp,
}

# Preferred node name first, legacy (pre-SFNC-2.0 GigE) fallback second.
EXPOSURE_NODE_CANDIDATES = ("ExposureTime", "ExposureTimeAbs")
GAIN_NODE_CANDIDATES = ("Gain", "GainRaw")


def sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


# Fixed GUI filename stems keyed by camera serial (string match). Unknown
# serials fall back to sanitize(model)_serial via camera_file_stem().
CAMERA_ALIASES_BY_SERIAL = {
    "40044823": "NorthCam",
    "40048976": "SouthCam",
    "40519358": "TopCam",
}


def camera_alias(serial: str) -> Optional[str]:
    """Return the fixed role alias for a known serial, or None if unknown."""
    return CAMERA_ALIASES_BY_SERIAL.get(str(serial))


def camera_file_stem(serial: str, model: str) -> str:
    """Filename stem for a GUI shot: known alias, else sanitize(model)_serial."""
    alias = camera_alias(serial)
    if alias:
        return alias
    return f"{sanitize(model)}_{serial}"


def _write_json_atomic(path: str, payload) -> None:
    """Write JSON via a temp file in the same directory, then os.replace.

    A plain open(path, "w") truncates first, so an interrupted write left a
    file that exists, is non-empty, and fails to parse — and every caller
    swallows the error, so the corruption surfaced later as a mystery.
    os.replace is atomic within a filesystem, so a reader sees either the
    old file or the new one, never a half-written one.
    """
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_shot_manifest(folder: str, entry: dict) -> None:
    """Write one Capture's session object to <folder>/capture_manifest.json
    (overwrite). Used by the GUI dated-shot path. Never raises to the caller."""
    path = os.path.join(folder, "capture_manifest.json")
    try:
        os.makedirs(folder, exist_ok=True)
        _write_json_atomic(path, entry)
    except Exception:
        LOG.exception("failed to write %s", path)


def read_node(node):
    """Safely read a GenICam parameter node (float/int/enum/etc.) whether
    it's genuinely implemented on this camera or a PlaceholderParameter
    (unimplemented feature). Returns None if unreadable.

    NOTE: node.GetValueOrDefault(default) is NOT safe for this — on a real
    (non-placeholder) typed parameter it requires a default of the exact
    matching C type and raises a TypeError otherwise (confirmed: passing
    None to a FloatParameter's GetValueOrDefault raises
    "argument 2 of type 'double'"). IsReadable()/GetValue() has no such
    type-matching requirement and is confirmed safe on both node kinds.
    """
    try:
        return node.GetValue() if node.IsReadable() else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Device discovery / selection
# ---------------------------------------------------------------------------

def discover_cameras() -> list:
    """Returns a list of pylon.DeviceInfo. Raises RuntimeError with a
    friendly message if zero devices are found."""
    tl_factory = pylon.TlFactory.GetInstance()
    devices = tl_factory.EnumerateDevices()
    if len(devices) == 0:
        raise RuntimeError(
            "No Basler cameras were detected. Check cables/power, confirm "
            "the camera(s) show up in Pylon Viewer, and (for USB3) confirm "
            "udev rules are installed."
        )
    return list(devices)


def print_camera_list(devices: list) -> None:
    print("\nDetected cameras:")
    for i, d in enumerate(devices):
        print(f"  [{i}] {d.GetModelName()}  (S/N {d.GetSerialNumber()})  [{d.GetDeviceClass()}]")
    print()


def resolve_camera_selection(devices: list, cli_value: Optional[str]) -> list:
    raw = cli_value if cli_value is not None else input(
        "Select camera(s) to use (comma-separated indices, or 'all'): "
    ).strip()
    if raw.lower() == "all":
        return list(range(len(devices)))
    indices = [int(x) for x in raw.split(",") if x.strip() != ""]
    if not indices:
        raise ValueError("No cameras selected.")
    for idx in indices:
        if not (0 <= idx < len(devices)):
            raise ValueError(f"Camera index {idx} is out of range (0-{len(devices) - 1}).")
    return indices


# ---------------------------------------------------------------------------
# Count / format / outdir resolution — CLI flags fall back to prompts
# ---------------------------------------------------------------------------

def resolve_count(cli_value: Optional[int]) -> int:
    if cli_value is not None:
        if cli_value <= 0:
            raise ValueError("--count must be a positive integer.")
        return cli_value
    while True:
        raw = input("How many images per camera? ").strip()
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
        print("Please enter a positive integer.")


def resolve_format(cli_value: Optional[str]) -> str:
    if cli_value is not None:
        return cli_value.lower()
    raw = input("Output format [tiff/png/bmp] (default: tiff): ").strip().lower()
    return raw if raw in FILE_FORMATS else "tiff"


def resolve_outdir(cli_value: Optional[str]) -> str:
    outdir = cli_value or input("Output directory (default: ./captures): ").strip() or "./captures"
    os.makedirs(outdir, exist_ok=True)
    return outdir


# ---------------------------------------------------------------------------
# Mono/color-aware converter setup
# ---------------------------------------------------------------------------

def _resolve_pixel_type(*names, fallback):
    """Return the first of `names` that exists as a pylon.PixelType_* constant
    in this pypylon build, else `fallback`. Defensive against pixel-type
    constants that may not exist in every pypylon version."""
    for name in names:
        pt = getattr(pylon, name, None)
        if pt is not None:
            return pt, name
    return fallback, None


def build_converter(camera) -> "pylon.ImageFormatConverter":
    # Read-only query of the camera's current pixel format — never written
    # here, so this never touches acquisition settings. Default to a color
    # assumption ("") only if the node is genuinely unreadable; startswith on
    # "" is False, which is a safe (if imperfect) fallback since running a
    # mono buffer through a color converter still yields a valid image,
    # whereas the reverse would mis-render a Bayer mosaic.
    fmt_name = read_node(camera.PixelFormat) or ""
    is_mono = fmt_name.startswith("Mono")

    # Bayer/mono format strings encode bit depth in their name, e.g. "Mono8",
    # "Mono12", "BayerRG12", "BayerRG12p" — extract it so we don't silently
    # discard bit depth above 8 bits (the whole point of defaulting to TIFF).
    depth_match = re.search(r"(\d+)", fmt_name)
    bit_depth = int(depth_match.group(1)) if depth_match else 8
    high_bit_depth = bit_depth > 8

    converter = pylon.ImageFormatConverter()
    converter.MaxNumThreads.SetToMaximum()

    downgraded = False
    if is_mono:
        if high_bit_depth:
            target, resolved_name = _resolve_pixel_type(
                "PixelType_Mono16", fallback=pylon.PixelType_Mono8
            )
            downgraded = resolved_name is None
        else:
            target = pylon.PixelType_Mono8
    else:
        if high_bit_depth:
            target, resolved_name = _resolve_pixel_type(
                "PixelType_BGR16packed", "PixelType_RGB16packed",
                fallback=pylon.PixelType_BGR8packed,
            )
            downgraded = resolved_name is None
        else:
            target = pylon.PixelType_BGR8packed

    if downgraded:
        LOG.warning(
            "%s: camera pixel format is %s (%d-bit) but this pypylon build "
            "has no matching 16-bit output pixel type — saving as 8-bit "
            "instead.", camera.DeviceInfo.GetSerialNumber(), fmt_name, bit_depth,
        )

    # OutputPixelFormat is a plain (non-node) Python property on
    # ImageFormatConverter — assign it directly. OutputBitAlignment IS a
    # GenICam-style node (like InstantCamera parameters) and needs .Value;
    # direct assignment still works but is deprecated as of pypylon 26.7.
    converter.OutputPixelFormat = target
    converter.OutputBitAlignment.Value = pylon.OutputBitAlignment_MsbAligned
    return converter


def log_camera_state(camera, label: str) -> None:
    """Read-only snapshot of settings this script never writes in the
    default flow — logged before/after grabbing so any drift is visible
    rather than assumed away."""

    def rd(*names, default="n/a"):
        for n in names:
            node = getattr(camera, n, None)
            if node is not None:
                val = read_node(node)
                if val is not None:
                    return val
        return default

    LOG.info(
        "[%s] %s: Exposure=%s Gain=%s PixelFormat=%s Size=%sx%s TriggerMode=%s",
        camera.DeviceInfo.GetSerialNumber(), label,
        rd(*EXPOSURE_NODE_CANDIDATES), rd(*GAIN_NODE_CANDIDATES),
        rd("PixelFormat"), rd("Width"), rd("Height"), rd("TriggerMode"),
    )


# ---------------------------------------------------------------------------
# Core per-camera grab-and-save loop (shared by CLI and GUI)
# ---------------------------------------------------------------------------

def capture_from_camera(camera, count: int, fmt: str, outdir: str, progress_cb=None,
                         shared_dir: Optional[str] = None,
                         file_stem: Optional[str] = None) -> CaptureResult:
    """Grab `count` frames and save them.

    CLI / default path (no `shared_dir` / `file_stem`): writes into
    `<outdir>/<model>_<serial>/` as
    `<model>_<serial>_<shot:04d>_<timestamp>.<fmt>`.

    GUI session path: pass `shared_dir` (the dated shot folder) and
    `file_stem` (e.g. NorthCam) so files land as
    `{file_stem}_{shot:03d}.{fmt}` inside that shared folder — no
    per-camera subfolder, no timestamp in the filename.

    Returns a CaptureResult reporting what was saved AGAINST what was asked
    for. It never raises for a per-camera failure — an unopenable camera or
    a mid-grab error comes back as `.error` with whatever was already
    written to disk still listed in `.saved`, so the caller can neither miss
    the failure nor lose the record of real files. Callers must consult
    `.ok`; a bare `.saved` cannot distinguish 10-of-10 from 3-of-10.
    """
    serial = camera.DeviceInfo.GetSerialNumber()
    model = sanitize(camera.DeviceInfo.GetModelName())
    if shared_dir:
        cam_dir = shared_dir
    else:
        cam_dir = os.path.join(outdir, f"{model}_{serial}")
    saved: list = []
    error: Optional[str] = None
    failed_grabs = 0

    try:
        os.makedirs(cam_dir, exist_ok=True)
        try:
            camera.Open()
        except Exception as e:
            raise RuntimeError(
                f"could not open camera {serial}: {e}. If Pylon Viewer (or "
                "another script) is still connected to this camera, close "
                "that connection first — only one exclusive connection is "
                "allowed at a time."
            ) from e
        log_camera_state(camera, "on-open")
        converter = build_converter(camera)

        # Drive the loop on frames SAVED, not frames retrieved.
        # StartGrabbingMax(count) bounds retrievals, so every incomplete
        # buffer used to burn one of the N slots and the loop exited short
        # — silently, since a short list is indistinguishable from a full
        # one. Grab open-endedly instead and stop at the target, with a
        # bounded attempt budget so sustained packet loss terminates rather
        # than spinning forever.
        camera.StartGrabbing(pylon.GrabStrategy_OneByOne)
        max_attempts = max(count * GRAB_ATTEMPT_MULTIPLIER, count + 3)
        attempts = 0
        warned_bit_depth = False
        while len(saved) < count and attempts < max_attempts and camera.IsGrabbing():
            attempts += 1
            try:
                with camera.RetrieveResult(
                    RETRIEVE_TIMEOUT_MS, pylon.TimeoutHandling_ThrowException
                ) as grab_result:
                    if not grab_result.GrabSucceeded():
                        failed_grabs += 1
                        LOG.warning("%s: grab error %#x %s", serial,
                                    grab_result.ErrorCode, grab_result.ErrorDescription)
                        continue

                    converted = converter.Convert(grab_result)
                    if not warned_bit_depth:
                        if not pylon.ImagePersistence.CanSaveWithoutConversion(
                            FILE_FORMATS[fmt], converted
                        ):
                            LOG.warning(
                                "%s: format '%s' may not preserve full bit depth for "
                                "this camera's images", serial, fmt,
                            )
                        warned_bit_depth = True

                    shot = len(saved) + 1
                    if file_stem:
                        filename = f"{file_stem}_{shot:03d}.{fmt}"
                    else:
                        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                        filename = f"{model}_{serial}_{shot:04d}_{ts}.{fmt}"
                    converted.Save(FILE_FORMATS[fmt], os.path.join(cam_dir, filename))
                    saved.append(filename)
                    if progress_cb:
                        progress_cb(len(saved), count)
            except pylon.TimeoutException as e:
                error = f"grab timeout after {len(saved)}/{count}: {e}"
                LOG.error("%s: %s", serial, error)
                break
            except Exception as e:
                # Previously only TimeoutException was caught, so a full disk
                # (ENOSPC on Save) or a dropped link (genicam.RuntimeException)
                # propagated out and discarded the record of frames already
                # written. Keep the record; report the failure.
                error = f"failed after {len(saved)}/{count}: {e}"
                LOG.exception("%s: %s", serial, error)
                break
        if error is None and len(saved) < count:
            error = (
                f"saved {len(saved)} of {count} requested "
                f"({failed_grabs} failed grab(s) in {attempts} attempts)"
            )
            LOG.error("%s: %s", serial, error)
        log_camera_state(camera, "post-grab")
    except Exception as e:
        error = str(e)
        LOG.error("%s: %s", serial, error)
    finally:
        # Each guarded separately: an exception from StopGrabbing must not
        # skip Close(), and neither must replace the error being reported.
        try:
            if camera.IsGrabbing():
                camera.StopGrabbing()
        except Exception:
            LOG.exception("%s: StopGrabbing failed", serial)
        try:
            if camera.IsOpen():
                camera.Close()
        except Exception:
            LOG.exception("%s: Close failed", serial)
    return CaptureResult(saved=saved, requested=count, error=error, serial=str(serial))




# ---------------------------------------------------------------------------
# CLI orchestration
# ---------------------------------------------------------------------------

def run_cli(args: argparse.Namespace) -> int:
    tl_factory = pylon.TlFactory.GetInstance()
    try:
        devices = discover_cameras()
    except RuntimeError as e:
        print(f"ERROR: {e}")
        return 1

    print_camera_list(devices)
    try:
        indices = resolve_camera_selection(devices, args.cameras)
        count = resolve_count(args.count)
        fmt = resolve_format(args.format)
        outdir = resolve_outdir(args.outdir)
    except (ValueError, OSError) as e:
        print(f"ERROR: {e}")
        return 1
    except EOFError:
        # A resolver fell back to input() with no stdin — the usual cause is
        # a partially-flagged invocation under cron/systemd/CI.
        print("ERROR: no input available for an omitted option. Pass "
              "--cameras, --count, --format and --outdir explicitly when "
              "running non-interactively.")
        return 1

    results: list = []
    for idx in indices:
        serial = str(devices[idx].GetSerialNumber())
        print(f"Capturing {count} image(s) from {devices[idx].GetModelName()} "
              f"(S/N {serial})...")
        try:
            camera = pylon.InstantCamera(tl_factory.CreateDevice(devices[idx]))
            results.append(capture_from_camera(camera, count, fmt, outdir))
        except Exception as e:
            LOG.error("Failed capturing from %s: %s", serial, e)
            print(f"  ERROR: {e}")
            results.append(CaptureResult(saved=[], requested=count,
                                          error=str(e), serial=serial))

    # Report what happened per camera, and let the exit code carry it. A run
    # that saved nothing used to print "Done." and exit 0, so a wrapper like
    # `capture_cameras ... && rsync ...` archived an empty directory as a
    # success.
    print()
    for r in results:
        print(f"  {'ok  ' if r.ok else 'FAIL'}  {r.summary()}")

    failed = [r for r in results if not r.ok]
    total_saved = sum(len(r.saved) for r in results)
    if failed:
        print(f"\n{len(failed)} of {len(results)} camera(s) failed or came up "
              f"short — {total_saved} image(s) saved. See messages above.")
        return 1
    print(f"\nDone. {total_saved} image(s) saved from {len(results)} camera(s).")
    return 0


# ---------------------------------------------------------------------------
# Optional Tkinter GUI: session capture (plate/bolts, BMP, dated shot folders)
# ---------------------------------------------------------------------------
# Visual shell is Dori-branded (navy header, teal primary, gray page, white
# rounded cards with pill controls). GUI capture is sequential into a shared
# dated shot folder. CLI is unchanged.

try:
    from PIL import Image, ImageTk
    _PIL_AVAILABLE = True
except ImportError:  # soft dep — GUI capture still works without JPEG previews
    Image = None  # type: ignore[misc, assignment]
    ImageTk = None  # type: ignore[misc, assignment]
    _PIL_AVAILABLE = False


def _bmp_to_preview_jpeg(bmp_path: str, jpeg_path: str,
                         max_width: int = 300, quality: int = 85) -> bool:
    """Convert a saved BMP to a small RGB JPEG preview. Worker-thread safe.
    Returns True on success. Basler BMPs may be odd/16-bit-ish modes — always
    convert to RGB before JPEG encode.
    """
    if not _PIL_AVAILABLE:
        return False
    try:
        with Image.open(bmp_path) as im:
            rgb = im.convert("RGB")
            w, h = rgb.size
            if w > max_width and w > 0:
                new_h = max(1, int(round(h * (max_width / float(w)))))
                rgb = rgb.resize((max_width, new_h), Image.Resampling.LANCZOS)
            rgb.save(jpeg_path, "JPEG", quality=quality, optimize=True)
        return True
    except Exception:
        LOG.exception("preview JPEG failed for %s", bmp_path)
        return False


DORI_PRIMARY = "#17B696"
DORI_NAVY = "#224C5C"
DORI_BG = "#EAEEF0"
DORI_TEXT = "#091116"
DORI_LOGO = "#2E5668"
DORI_CARD = "#FFFFFF"
DORI_STATUS_OK = "#00695C"
DORI_STATUS_WARN = "#FFAB40"
DORI_STATUS_ERR = "#B71C1C"
DORI_MUTED = "#5A6E78"
DORI_BORDER = "#D0D7DC"
DORI_PRIMARY_HOVER = "#129A80"
DORI_NAVY_HOVER = "#1A3C48"
DORI_SHADOW = "#C5CFD4"
DORI_SUBTITLE = "#9BB4BC"
DORI_LOGO_WELL = "#1A3A46"

_UI_FONT_CANDIDATES = (
    "Helvetica Neue",
    ".AppleSystemUIFont",
    "SF Pro Text",
    "Segoe UI",
    "Helvetica",
)


def _ui_font_family(tkfont) -> str:
    """Prefer a clean UI face; fall back to Tk's default if none are present."""
    available = set(tkfont.families())
    for name in _UI_FONT_CANDIDATES:
        if name in available:
            return name
    return tkfont.nametofont("TkDefaultFont").actual()["family"]


def _round_rect_coords(x1, y1, x2, y2, r):
    r = max(0.0, min(float(r), abs(x2 - x1) / 2.0, abs(y2 - y1) / 2.0))
    return (
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    )


def _canvas_round_rect(canvas, x1, y1, x2, y2, r, **kwargs):
    return canvas.create_polygon(
        _round_rect_coords(x1, y1, x2, y2, r), smooth=True, **kwargs,
    )


def _ui_config_dir() -> str:
    """User config dir for GUI prefs (logo path). Linux/mac: ~/.config or
    $XDG_CONFIG_HOME; Windows: %APPDATA%."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "script-grabber")
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return os.path.join(xdg, "script-grabber")
    return os.path.join(os.path.expanduser("~"), ".config", "script-grabber")


def _ui_prefs_path() -> str:
    return os.path.join(_ui_config_dir(), "ui.json")


def _load_ui_prefs() -> dict:
    path = _ui_prefs_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_ui_prefs(prefs: dict) -> None:
    path = _ui_prefs_path()
    try:
        os.makedirs(_ui_config_dir(), exist_ok=True)
        _write_json_atomic(path, prefs)
    except Exception:
        LOG.exception("failed to write %s", path)


def _status_annotation_color(msg: str) -> str:
    """Map existing status strings onto Dori annotation colors. Does not
    rewrite backend messages — only parses them."""
    lower = (msg or "").lower()
    if "error" in lower or "could not" in lower or "rejected" in lower:
        return DORI_STATUS_ERR
    if "no cameras" in lower:
        return DORI_STATUS_WARN
    if (
        lower.startswith("ready")
        or "capture complete" in lower
        or lower.startswith("settings applied")
        or (lower.startswith("found ") and "camera" in lower)
    ):
        return DORI_STATUS_OK
    return DORI_TEXT


def _apply_dori_theme(style, family: str) -> None:
    """ttk Style for inputs, labels, and checkbuttons. Buttons are Canvas
    pills drawn separately — 'clam' still gives Entry/Combobox a paintable
    field background on Linux/macOS/Windows."""
    try:
        style.theme_use("clam")
    except Exception:
        pass

    style.configure(".", background=DORI_BG, foreground=DORI_TEXT, font=(family, 11))
    style.configure("TFrame", background=DORI_BG)
    style.configure("TLabel", background=DORI_BG, foreground=DORI_TEXT, font=(family, 11))
    style.configure("TCheckbutton", background=DORI_BG, foreground=DORI_TEXT, font=(family, 11))
    style.configure(
        "TEntry",
        fieldbackground=DORI_CARD,
        foreground=DORI_TEXT,
        background=DORI_CARD,
        bordercolor=DORI_BORDER,
        lightcolor=DORI_BORDER,
        darkcolor=DORI_BORDER,
        insertcolor=DORI_TEXT,
        padding=6,
    )
    style.configure(
        "TCombobox",
        fieldbackground=DORI_CARD,
        background=DORI_CARD,
        foreground=DORI_TEXT,
        arrowcolor=DORI_NAVY,
        bordercolor=DORI_BORDER,
        padding=6,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", DORI_CARD)],
        foreground=[("readonly", DORI_TEXT)],
        bordercolor=[("focus", DORI_NAVY), ("readonly", DORI_BORDER)],
    )

    style.configure("Card.TFrame", background=DORI_CARD)
    style.configure("Card.TLabel", background=DORI_CARD, foreground=DORI_TEXT, font=(family, 11))
    style.configure("CardMuted.TLabel", background=DORI_CARD, foreground=DORI_MUTED, font=(family, 10))
    style.configure(
        "Card.TCheckbutton",
        background=DORI_CARD,
        foreground=DORI_TEXT,
        font=(family, 11),
        focuscolor=DORI_CARD,
    )
    style.map(
        "Card.TCheckbutton",
        background=[("active", DORI_CARD), ("selected", DORI_CARD)],
        foreground=[("disabled", DORI_MUTED)],
    )
    style.configure("Bar.TLabel", background=DORI_CARD, foreground=DORI_MUTED, font=(family, 8))


def run_gui() -> int:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import ttk, filedialog, messagebox

    tl_factory = pylon.TlFactory.GetInstance()

    def discover_or_empty() -> list:
        # The GUI must still open with zero cameras (none connected/powered
        # on yet, or a network hiccup) — discover_cameras() raising
        # RuntimeError for "no devices" is a normal, expected state here,
        # not a fatal error like it is for the CLI path.
        try:
            return discover_cameras()
        except RuntimeError:
            return []

    devices = discover_or_empty()

    root = tk.Tk()
    root.title("Script Grabber")
    root.configure(bg=DORI_BG)
    root.minsize(820, 600)
    root.geometry("900x640")

    family = _ui_font_family(tkfont)
    style = ttk.Style(root)
    _apply_dori_theme(style, family)
    style.configure(
        "Card.TRadiobutton",
        background=DORI_CARD,
        foreground=DORI_TEXT,
        font=(family, 11),
        focuscolor=DORI_CARD,
    )
    style.map(
        "Card.TRadiobutton",
        background=[("active", DORI_CARD), ("selected", DORI_CARD)],
        foreground=[("disabled", DORI_MUTED)],
    )
    title_font = tkfont.Font(family=family, size=18, weight="normal")
    subtitle_font = tkfont.Font(family=family, size=11, weight="normal")
    caps_font = tkfont.Font(family=family, size=8, weight="normal")
    pill_font = tkfont.Font(family=family, size=11, weight="normal")
    status_font = tkfont.Font(family=family, size=9, weight="normal")

    def _rounded_panel(parent, fill=DORI_CARD, radius=14, shadow=True, canvas_bg=None):
        """Canvas round-rect chrome + inner Frame. Returns (outer, inner)."""
        bg = canvas_bg if canvas_bg is not None else parent.cget("bg")
        outer = tk.Frame(parent, bg=bg)
        canvas = tk.Canvas(outer, bg=bg, highlightthickness=0, bd=0)
        canvas.place(x=0, y=0, relwidth=1, relheight=1)
        inner = tk.Frame(outer, bg=fill)
        off = 2 if shadow else 0
        inset = max(6, int(radius * 0.35))
        inner.pack(
            fill="both", expand=True,
            padx=(inset, inset + off), pady=(inset, inset + off),
        )

        def _redraw(event):
            if event.widget is not outer:
                return
            w, h = event.width, event.height
            canvas.delete("chrome")
            if w < 8 or h < 8:
                return
            if shadow:
                _canvas_round_rect(
                    canvas, off, off, w, h, radius,
                    fill=DORI_SHADOW, outline="", tags="chrome",
                )
            _canvas_round_rect(
                canvas, 0, 0, max(w - off, 1), max(h - off, 1), radius,
                fill=fill, outline="", tags="chrome",
            )
            # tkinter aliases Canvas.lower to tag_lower (lower a canvas ITEM),
            # so the bare canvas.lower() this used to call raised TclError
            # "wrong # args" on every <Configure> of every panel, and the
            # widget was never restacked. Misc.lower is the widget operation.
            tk.Misc.lower(canvas)

        outer.bind("<Configure>", _redraw)
        return outer, inner

    def _pill_button(parent, text, command, kind="outline", canvas_bg=None):
        """Canvas pill: solid teal Capture, or navy-outline secondary."""
        bg = canvas_bg if canvas_bg is not None else parent.cget("bg")
        pad_x = 20 if kind == "solid" else 16
        pad_y = 8 if kind == "solid" else 7
        tw = pill_font.measure(text)
        th = pill_font.metrics("linespace")
        w = max(int(tw + pad_x * 2), 92)
        h = int(th + pad_y * 2)
        r = h / 2.0
        cnv = tk.Canvas(
            parent, width=w, height=h, bg=bg,
            highlightthickness=0, bd=0, cursor="hand2",
        )
        hover = [False]
        enabled = [True]

        def draw(_event=None):
            cnv.delete("all")
            if not enabled[0]:
                # Muted, no hover, no pointer cursor — the button must LOOK
                # inert while a capture runs, not just ignore clicks.
                _canvas_round_rect(cnv, 0, 0, w, h, r, fill=DORI_BORDER, outline="")
                cnv.create_text(w / 2, h / 2, text=text, fill=DORI_MUTED, font=pill_font)
            elif kind == "solid":
                fill = DORI_PRIMARY_HOVER if hover[0] else DORI_PRIMARY
                _canvas_round_rect(cnv, 0, 0, w, h, r, fill=fill, outline="")
                cnv.create_text(w / 2, h / 2, text=text, fill="#FFFFFF", font=pill_font)
            else:
                fill = "#F3F6F7" if hover[0] else DORI_CARD
                _canvas_round_rect(
                    cnv, 0.5, 0.5, w - 0.5, h - 0.5, r, fill=DORI_NAVY, outline="",
                )
                _canvas_round_rect(
                    cnv, 1.8, 1.8, w - 1.8, h - 1.8, max(r - 1.6, 1),
                    fill=fill, outline="",
                )
                cnv.create_text(w / 2, h / 2, text=text, fill=DORI_NAVY, font=pill_font)

        def set_enabled(on: bool):
            enabled[0] = bool(on)
            hover[0] = False
            cnv.configure(cursor="hand2" if on else "")
            draw()

        cnv.bind("<Enter>", lambda _e: enabled[0] and (hover.__setitem__(0, True), draw()))
        cnv.bind("<Leave>", lambda _e: (hover.__setitem__(0, False), draw()))
        cnv.bind("<Button-1>", lambda _e: command() if enabled[0] else None)
        cnv.set_enabled = set_enabled  # type: ignore[attr-defined]
        draw()
        return cnv

    status_var = tk.StringVar(
        value="Ready." if devices else "No cameras detected. Connect a camera and click Rescan."
    )
    ui_queue: "queue.Queue" = queue.Queue()
    capturing = [False]  # list-boxed so nested functions can mutate it without `nonlocal`
    capture_btn = [None]  # the Capture pill, once built (see set_capture_enabled)

    def set_capture_enabled(on: bool):
        """Single owner of the Capture button's enabled state, so the button,
        the `capturing` flag and the Rescan guard cannot disagree."""
        if capture_btn[0] is not None:
            capture_btn[0].set_enabled(on)

    cam_vars = []

    # --- Header (navy) + hairline teal accent ------------------------------
    header = tk.Frame(root, bg=DORI_NAVY)
    header.pack(fill="x")

    title_row = tk.Frame(header, bg=DORI_NAVY)
    title_row.pack(side="left", padx=28, pady=16)
    tk.Label(
        title_row, text="Script Grabber", bg=DORI_NAVY, fg="#FFFFFF",
        font=title_font,
    ).pack(side="left")
    tk.Label(
        title_row, text=f"Multi-camera capture  ·  v{__version__}",
        bg=DORI_NAVY, fg=DORI_SUBTITLE, font=subtitle_font,
    ).pack(side="left", padx=(14, 0), pady=(3, 0))

    logo_slot = tk.Frame(header, bg=DORI_NAVY)
    logo_slot.pack(side="right", padx=24, pady=12)
    logo_photo = [None]  # PhotoImage must be held or Tk garbage-collects it
    LOGO_WELL_W, LOGO_WELL_H, LOGO_WELL_R = 128, 40, 12

    def choose_logo(_event=None):
        path = filedialog.askopenfilename(
            title="Choose customer logo",
            filetypes=[
                ("PNG, GIF, PPM", "*.png *.gif *.ppm *.pgm"),
                ("PNG", "*.png"),
                ("GIF", "*.gif"),
                ("PPM / PGM", "*.ppm *.pgm"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        try:
            tk.PhotoImage(file=path)
        except Exception as e:
            messagebox.showwarning(
                "Unsupported image",
                "Could not load that file with Tk PhotoImage.\n"
                "Use PNG, GIF, or PPM/PGM (no JPEG unless Tk was built with it).\n\n"
                f"{e}",
            )
            return
        prefs = _load_ui_prefs()
        prefs["customer_logo_path"] = path
        _save_ui_prefs(prefs)
        refresh_logo()

    def refresh_logo():
        for child in logo_slot.winfo_children():
            child.destroy()
        logo_photo[0] = None
        path = (_load_ui_prefs().get("customer_logo_path") or "").strip()
        img = None
        if path and os.path.isfile(path):
            try:
                img = tk.PhotoImage(file=path)
                h, w = img.height(), img.width()
                factor = 1
                while factor < 32 and (h // factor > 28 or w // factor > 108):
                    factor += 1
                if factor > 1:
                    img = img.subsample(factor, factor)
            except Exception:
                img = None
        well = tk.Canvas(
            logo_slot, width=LOGO_WELL_W, height=LOGO_WELL_H,
            bg=DORI_NAVY, highlightthickness=0, bd=0, cursor="hand2",
        )
        well.pack()
        _canvas_round_rect(
            well, 0, 0, LOGO_WELL_W, LOGO_WELL_H, LOGO_WELL_R,
            fill=DORI_LOGO_WELL, outline="",
        )
        if img is not None:
            logo_photo[0] = img
            well.create_image(LOGO_WELL_W / 2, LOGO_WELL_H / 2, image=img)
        else:
            well.create_text(
                LOGO_WELL_W / 2, LOGO_WELL_H / 2,
                text="ADD LOGO", fill=DORI_LOGO, font=caps_font,
            )
        well.bind("<Button-1>", choose_logo)

    refresh_logo()

    tk.Frame(root, bg=DORI_PRIMARY, height=2).pack(fill="x")

    # NOTE ON PACK ORDER: the control bar and status line are packed to the
    # bottom BEFORE `content` claims the remaining space with expand=True.
    # Packed after it, pack reserved nothing for them, so the growing camera
    # list and preview strip pushed them out of the window entirely — with
    # three cameras the status line was already unmapped at the default
    # 900x640, and after a capture the Capture/Rescan bar went with it,
    # leaving no way to start another run. The placeholders below are filled
    # in further down, once their callbacks exist.
    status_row = tk.Frame(root, bg=DORI_BG)
    status_row.pack(side="bottom", fill="x", padx=28, pady=(0, 12))
    bar_slot = tk.Frame(root, bg=DORI_BG)
    bar_slot.pack(side="bottom", fill="x")

    content = tk.Frame(root, bg=DORI_BG)
    content.pack(fill="both", expand=True, padx=28, pady=(20, 16))

    cams_frame = tk.Frame(content, bg=DORI_BG)
    cams_frame.pack(fill="both", expand=True)

    def build_camera_rows():
        # Rebuildable so Rescan can pick up cameras connected after the
        # window was already opened, without restarting the GUI. Mutates
        # devices/cam_vars IN PLACE (clear + re-populate) rather than
        # rebinding them, so every other closure reading these list objects
        # by reference sees the rebuilt contents automatically.
        for child in cams_frame.winfo_children():
            child.destroy()
        cam_vars.clear()

        if not devices:
            empty_outer, empty_inner = _rounded_panel(
                cams_frame, fill=DORI_CARD, radius=16, shadow=True,
            )
            empty_outer.pack(fill="both", expand=True, pady=(0, 4))
            empty_inner.grid_rowconfigure(0, weight=1)
            empty_inner.grid_columnconfigure(0, weight=1)
            empty_body = tk.Frame(empty_inner, bg=DORI_CARD)
            empty_body.grid(row=0, column=0, padx=48, pady=48)
            tk.Label(
                empty_body, text="No cameras yet", bg=DORI_CARD, fg=DORI_NAVY,
                font=(family, 13),
            ).pack()
            tk.Label(
                empty_body,
                text="They will appear here when a Basler camera is on the network.",
                bg=DORI_CARD, fg=DORI_MUTED, font=(family, 11),
            ).pack(pady=(8, 0))
            return

        for i, d in enumerate(devices):
            card, inner = _rounded_panel(
                cams_frame, fill=DORI_CARD, radius=14, shadow=True,
            )
            card.pack(fill="x", pady=(0, 10))
            inner.pack_configure(padx=(16, 18), pady=(12, 14))

            top = tk.Frame(inner, bg=DORI_CARD)
            top.pack(fill="x")
            v = tk.BooleanVar(value=True)
            cam_vars.append(v)
            serial = str(d.GetSerialNumber())
            model_name = d.GetModelName()
            alias = camera_alias(serial)
            if alias:
                cb_text = f"{alias} — {model_name}"
            else:
                cb_text = model_name
            ttk.Checkbutton(
                top, text=cb_text, variable=v, style="Card.TCheckbutton",
            ).pack(side="left")
            ttk.Label(
                top, text=f"S/N {serial}", style="CardMuted.TLabel",
            ).pack(side="left", padx=(10, 0))
            if alias:
                badge = tk.Label(
                    top, text=alias, bg="#E8F5F1", fg=DORI_PRIMARY,
                    font=(family, 9), padx=8, pady=1,
                )
                badge.pack(side="right")

    build_camera_rows()

    hint_row = tk.Frame(content, bg=DORI_BG)
    hint_row.pack(fill="x", pady=(4, 0))
    tk.Label(
        hint_row,
        text="Set plate color & bolts size, then Capture → "
             "<folder>/<MM-DD>/<Plate>_<Bolts>/<HHMMSS>/ "
             "(e.g. Black_Big). All cams land in that shot folder.",
        bg=DORI_BG, fg=DORI_MUTED, font=(family, 10), anchor="w", justify="left",
    ).pack(side="left", fill="x", expand=True)

    # --- Post-capture JPEG preview strip ----------------------------------
    preview_outer, preview_inner = _rounded_panel(
        content, fill=DORI_CARD, radius=14, shadow=True,
    )
    preview_outer.pack(fill="x", pady=(12, 0))
    preview_pad = tk.Frame(preview_inner, bg=DORI_CARD)
    preview_pad.pack(fill="x", padx=12, pady=10)
    tk.Label(
        preview_pad, text="PREVIEWS", bg=DORI_CARD, fg=DORI_MUTED, font=caps_font,
    ).pack(anchor="w")
    preview_strip = tk.Frame(preview_pad, bg=DORI_CARD)
    preview_strip.pack(fill="x", pady=(8, 0))
    preview_photos: list = []  # hold ImageTk/PhotoImage refs so Tk won't GC them
    pillow_warned = [False]

    def _clear_preview_strip():
        for child in preview_strip.winfo_children():
            child.destroy()
        preview_photos.clear()

    def show_preview_empty():
        """Quiet empty card — shown before any capture and on Rescan."""
        _clear_preview_strip()
        tk.Label(
            preview_strip,
            text="Previews appear after Capture",
            bg=DORI_CARD, fg=DORI_MUTED, font=(family, 10),
        ).pack(anchor="w")

    def add_preview_tile(alias: str, jpeg_path: str) -> None:
        """Main-thread only. Load JPEG into the wrapping horizontal strip."""
        if not _PIL_AVAILABLE or ImageTk is None:
            return
        # Drop the empty-state label on the first real preview.
        for child in list(preview_strip.winfo_children()):
            if isinstance(child, tk.Label) and child.cget("text") == (
                "Previews appear after Capture"
            ):
                child.destroy()
        try:
            with Image.open(jpeg_path) as im:
                rgb = im.convert("RGB")
                photo = ImageTk.PhotoImage(rgb)
        except Exception as e:
            LOG.exception("could not load preview %s", jpeg_path)
            status_var.set(f"Preview load failed for {alias}: {e}")
            return
        preview_photos.append(photo)
        tile = tk.Frame(preview_strip, bg=DORI_CARD)
        tile.pack(side="left", padx=(0, 12), pady=(0, 4))
        tk.Label(tile, image=photo, bg=DORI_CARD).pack()
        tk.Label(
            tile, text=alias, bg=DORI_CARD, fg=DORI_NAVY, font=(family, 10),
        ).pack(pady=(4, 0))

    show_preview_empty()

    # --- Session fields (plate / bolts) ------------------------------------
    session_outer, session_inner = _rounded_panel(
        content, fill=DORI_CARD, radius=14, shadow=True,
    )
    session_outer.pack(fill="x", pady=(12, 0))
    session_pad = tk.Frame(session_inner, bg=DORI_CARD)
    session_pad.pack(fill="x", padx=12, pady=10)

    plate_color_var = tk.StringVar(value="Black")
    bolts_size_var = tk.StringVar(value="Big")

    def _session_choice_row(parent, label, var, choices):
        row = tk.Frame(parent, bg=DORI_CARD)
        row.pack(fill="x", pady=(0, 8))
        tk.Label(
            row, text=label, bg=DORI_CARD, fg=DORI_MUTED, font=caps_font,
        ).pack(side="left")
        for choice in choices:
            ttk.Radiobutton(
                row, text=choice, value=choice, variable=var,
                style="Card.TRadiobutton",
            ).pack(side="left", padx=(14 if choice == choices[0] else 10, 0))
        return row

    _session_choice_row(session_pad, "PLATE COLOR", plate_color_var, ("Black", "Silver"))
    last = _session_choice_row(session_pad, "BOLTS SIZE", bolts_size_var, ("Big", "Small"))
    last.pack_configure(pady=(0, 0))

    count_var = tk.IntVar(value=10)
    outdir_var = tk.StringVar(value=os.path.abspath("./captures"))

    # Capture runs in a worker thread; Tk widgets must only ever be touched
    # from the main thread, so the worker pushes status strings onto a queue
    # that a root.after loop drains on the GUI thread.
    def worker(chosen_devices, count, outdir, plate_color, bolts_size):
        # `chosen_devices` is a snapshot taken on the main thread, not indices
        # into the shared `devices` list — Rescan replaces that list in place,
        # and indexing it from here raced with that.
        failures: list = []
        cameras_meta: list = []
        shot_folder = None
        try:
            now = datetime.datetime.now()
            # Year included: %m-%d alone made lexical and chronological order
            # diverge across a year boundary, and an anniversary-date shot
            # landed in the previous year's combo folder.
            date_folder = now.strftime("%Y-%m-%d")
            # One folder per plate/bolts combo under the date, then one shot
            # folder per Capture click — so opening a date shows Black_Big /
            # Silver_Small / etc., and each press nests under the matching combo.
            combo_folder = f"{sanitize(plate_color)}_{sanitize(bolts_size)}"
            time_folder = now.strftime("%H%M%S")
            shot_folder = os.path.join(outdir, date_folder, combo_folder, time_folder)
            os.makedirs(shot_folder, exist_ok=True)
            ui_queue.put(f"Shot folder: {shot_folder}")

            fmt = "bmp"

            def flush_manifest():
                """Rewritten after every camera, so a run that is interrupted
                — window closed, process killed — still leaves a record of
                what those BMPs are of."""
                write_shot_manifest(shot_folder, {
                    "plate_color": plate_color,
                    "bolts_size": bolts_size,
                    "combo_folder": combo_folder,
                    "timestamp": now.isoformat(timespec="seconds"),
                    "shot_folder": shot_folder,
                    "cameras": cameras_meta,
                    "count": count,
                    "format": fmt,
                    "complete": len(cameras_meta) == len(chosen_devices),
                })

            for d in chosen_devices:
                serial = str(d.GetSerialNumber())
                model_raw = d.GetModelName()
                alias = camera_alias(serial)
                stem = camera_file_stem(serial, model_raw)
                label = alias or model_raw

                def cb(shot, total, label=label):
                    ui_queue.put(f"{label}: {shot}/{total}")

                try:
                    cam = pylon.InstantCamera(tl_factory.CreateDevice(d))
                    result = capture_from_camera(
                        cam, count, fmt, outdir, progress_cb=cb,
                        shared_dir=shot_folder, file_stem=stem,
                    )
                except Exception as e:
                    result = CaptureResult(saved=[], requested=count,
                                            error=str(e), serial=serial)

                files = result.saved
                if not result.ok:
                    failures.append(f"{label}: {result.error}")
                    ui_queue.put(f"{label}: ERROR {result.error}")

                display_alias = alias or stem
                cameras_meta.append({
                    "serial": serial,
                    "model": model_raw,
                    "alias": display_alias,
                    "files": files,
                    "requested": count,
                    "saved": len(files),
                    "error": result.error,
                })
                flush_manifest()

                # JPEG preview from the first saved BMP (Alias_001.bmp). File
                # I/O stays on this worker thread; only the path is handed to Tk.
                if files:
                    if not _PIL_AVAILABLE:
                        ui_queue.put("__NO_PILLOW__")
                    else:
                        bmp_path = os.path.join(shot_folder, files[0])
                        jpeg_path = os.path.join(
                            shot_folder, f"preview_{display_alias}.jpg",
                        )
                        if _bmp_to_preview_jpeg(bmp_path, jpeg_path):
                            ui_queue.put(("__PREVIEW__", display_alias, jpeg_path))
        except Exception as e:
            # Anything outside the per-camera try — creating the shot folder
            # on a read-only mount, most likely. This used to kill the thread
            # before __DONE__, stranding capturing[0] True for the life of the
            # process and blocking Rescan forever.
            LOG.exception("capture worker failed")
            failures.append(f"capture failed: {e}")
        finally:
            # __DONE__ in a finally, always, whatever happened above.
            ui_queue.put(("__DONE__", failures))

    def poll_queue():
        try:
            while True:
                msg = ui_queue.get_nowait()
                # The completion sentinel carries the run's failures with it.
                # Reporting them through status_var alone could never work:
                # this loop drains the whole queue inside one Tk callback and
                # Tk does not redraw mid-callback, so every error string was
                # overwritten by "Capture complete." before it was ever painted.
                if isinstance(msg, tuple) and msg and msg[0] == "__DONE__":
                    capturing[0] = False
                    set_capture_enabled(True)
                    failures = msg[1] if len(msg) > 1 else []
                    if failures:
                        status_var.set(
                            f"Capture finished with {len(failures)} error(s): "
                            + "; ".join(failures)
                        )
                        messagebox.showerror(
                            "Capture incomplete",
                            f"{len(failures)} of the selected cameras failed or "
                            "saved fewer images than requested:\n\n"
                            + "\n".join(failures),
                        )
                    elif pillow_warned[0]:
                        status_var.set(
                            "Capture complete. Pillow not installed — JPEG "
                            "previews skipped."
                        )
                    else:
                        status_var.set("Capture complete.")
                elif msg == "__NO_PILLOW__":
                    # Latch only; the message is folded into the completion
                    # line above, which is the one that survives the drain.
                    pillow_warned[0] = True
                elif (
                    isinstance(msg, tuple)
                    and len(msg) == 3
                    and msg[0] == "__PREVIEW__"
                ):
                    _alias, _jpeg = msg[1], msg[2]
                    add_preview_tile(_alias, _jpeg)
                else:
                    status_var.set(msg)
        except queue.Empty:
            pass
        finally:
            # In a finally so a raise while draining cannot silently kill the
            # polling loop and freeze every future status update.
            root.after(100, poll_queue)

    def start_capture():
        if capturing[0]:
            return  # a capture is already running; the button is disabled too
        indices = [i for i, v in enumerate(cam_vars) if v.get()]
        if not indices:
            messagebox.showwarning("No cameras", "Select at least one camera.")
            return
        try:
            count = int(count_var.get())
            if count <= 0:
                raise ValueError
        except (tk.TclError, ValueError):
            messagebox.showwarning("Invalid count", "Count must be a positive integer.")
            return

        main_outdir = os.path.expanduser(outdir_var.get().strip()) or os.path.abspath("./captures")
        main_outdir = os.path.abspath(main_outdir)
        outdir_var.set(main_outdir)
        try:
            os.makedirs(main_outdir, exist_ok=True)
            # exist_ok short-circuits on an existing directory without testing
            # writability, so an existing read-only folder passed this check
            # and then failed in the worker instead.
            if not os.access(main_outdir, os.W_OK):
                raise PermissionError(f"not writable: {main_outdir}")
        except OSError as e:
            messagebox.showerror(
                "Cannot use that folder",
                f"{main_outdir}\n\n{e}\n\nPick a different output folder.",
            )
            return

        # Snapshot the chosen devices now, on the main thread; Rescan mutates
        # the shared `devices` list and the worker must not index into it.
        chosen = [devices[i] for i in indices if i < len(devices)]
        if not chosen:
            messagebox.showwarning("No cameras", "Select at least one camera.")
            return

        capturing[0] = True
        set_capture_enabled(False)
        _clear_preview_strip()  # refill as each camera finishes
        threading.Thread(
            target=worker,
            args=(
                chosen, count, main_outdir,
                plate_color_var.get(), bolts_size_var.get(),
            ),
            daemon=True,
        ).start()

    def rescan():
        if capturing[0]:
            messagebox.showwarning(
                "Capture in progress",
                "Wait for the current capture to finish before rescanning.",
            )
            return
        devices[:] = discover_or_empty()
        build_camera_rows()
        show_preview_empty()
        if devices:
            status_var.set(f"Found {len(devices)} camera(s).")
        else:
            status_var.set("No cameras detected. Connect a camera and click Rescan.")

    bar, bar_inner = _rounded_panel(
        bar_slot, fill=DORI_CARD, radius=16, shadow=True, canvas_bg=DORI_BG,
    )
    bar.pack(fill="x", padx=28, pady=(0, 8))
    bar_pad = tk.Frame(bar_inner, bg=DORI_CARD)
    bar_pad.pack(fill="x", padx=12, pady=10)

    row1 = tk.Frame(bar_pad, bg=DORI_CARD)
    row1.pack(fill="x")

    def _caps(parent, text):
        return tk.Label(
            parent, text=text, bg=DORI_CARD, fg=DORI_MUTED, font=caps_font,
        )

    _caps(row1, "COUNT").pack(side="left")
    ttk.Entry(row1, textvariable=count_var, width=6).pack(side="left", padx=(8, 0))
    _caps(row1, "FORMAT").pack(side="left", padx=(20, 0))
    tk.Label(
        row1, text="BMP", bg=DORI_CARD, fg=DORI_TEXT, font=(family, 11),
    ).pack(side="left", padx=(8, 0))
    _caps(row1, "FOLDER").pack(side="left", padx=(20, 0))
    ttk.Entry(row1, textvariable=outdir_var).pack(
        side="left", fill="x", expand=True, padx=(8, 10),
    )
    _pill_button(
        row1, "Browse",
        lambda: outdir_var.set(filedialog.askdirectory() or outdir_var.get()),
        kind="outline", canvas_bg=DORI_CARD,
    ).pack(side="left")

    row2 = tk.Frame(bar_pad, bg=DORI_CARD)
    row2.pack(fill="x", pady=(14, 0))
    capture_btn[0] = _pill_button(
        row2, "Capture", start_capture, kind="solid", canvas_bg=DORI_CARD,
    )
    capture_btn[0].pack(side="right")
    _pill_button(
        row2, "Rescan", rescan, kind="outline", canvas_bg=DORI_CARD,
    ).pack(side="right", padx=(0, 10))

    status_label = tk.Label(
        status_row, textvariable=status_var, anchor="w", justify="left",
        bg=DORI_BG, fg=_status_annotation_color(status_var.get()),
        font=status_font, wraplength=800,
    )
    status_label.pack(fill="x")

    def _on_status_write(*_args):
        status_label.configure(foreground=_status_annotation_color(status_var.get()))

    status_var.trace_add("write", _on_status_write)

    def _on_root_configure(event):
        if event.widget is root:
            wrap = max(event.width - 72, 200)
            status_label.configure(wraplength=wrap)

    root.bind("<Configure>", _on_root_configure)

    def _on_close():
        # The capture worker is a daemon thread: closing the window returns
        # from mainloop and the interpreter stops it wherever it happens to
        # be. The shot manifest is now rewritten after every camera, so an
        # interrupted run still documents what it captured — but confirm
        # anyway rather than silently truncating a run in progress.
        if capturing[0]:
            if not messagebox.askokcancel(
                "Capture in progress",
                "A capture is still running. Closing now stops it — images "
                "already written stay on disk and the shot manifest records "
                "what completed.\n\nClose anyway?",
            ):
                return
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)

    poll_queue()
    root.mainloop()
    return 0



# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--cameras", help="Comma-separated indices, or 'all'")
    parser.add_argument("--count", type=int, help="Images per camera")
    parser.add_argument("--format", choices=list(FILE_FORMATS), help="Output format")
    parser.add_argument("--outdir", help="Output directory")
    parser.add_argument("--gui", action="store_true", help="Launch combined Tkinter GUI")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.gui:
        return run_gui()
    return run_cli(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(130)
