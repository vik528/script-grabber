#!/usr/bin/env python3
"""
capture_cameras.py — Multi-camera capture utility for Basler cameras
(GigE / USB3, via pypylon / the Pylon SDK).

Auto-detects every Basler camera pypylon can see, lets you pick which ones to
use, and captures a chosen number of images from each. Camera settings
(exposure, gain, etc.) are left exactly as configured in Pylon Viewer — this
script never writes acquisition parameters in its default (CLI) flow. Run
with --gui for an optional session-oriented window (plate/bolts fields,
BMP-only, dated shot folders, fixed camera aliases — see README).

Usage:
    python3 capture_cameras.py
    python3 capture_cameras.py --cameras all --count 10 --format tiff --outdir ./captures
    python3 capture_cameras.py --gui

Testing without hardware: set PYLON_CAMEMU=2 (or more) before running to
have pypylon emulate that many virtual cameras.
"""
from __future__ import annotations

__version__ = "1.3.0"

import argparse
import datetime
import json
import logging
import os
import queue
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

try:
    from pypylon import pylon, genicam
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


def append_manifest_entry(folder: str, entry: dict) -> None:
    """Append one entry to <folder>/capture_manifest.json (created as an
    empty list if absent) — session documentation only (lens info, group
    membership), never anything read back or used to drive capture. Never
    raises to the caller: a manifest write failure must not abort a capture
    that otherwise succeeded."""
    path = os.path.join(folder, "capture_manifest.json")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = []
    except Exception:
        LOG.exception("could not read existing %s — starting a fresh list", path)
        data = []
    data.append(entry)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        LOG.exception("failed to write %s", path)


def write_shot_manifest(folder: str, entry: dict) -> None:
    """Write one Capture's session object to <folder>/capture_manifest.json
    (overwrite). Used by the GUI dated-shot path. Never raises to the caller."""
    path = os.path.join(folder, "capture_manifest.json")
    try:
        os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entry, f, indent=2)
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


def partition_into_groups(indices: list, group_of) -> list:
    """Partition `indices` (already filtered to checked/selected cameras)
    into ordered sub-lists sharing the same Group field value, in order of
    first appearance. `group_of(idx)` returns the raw (string) Group value
    for that camera index. Blank/whitespace-only values are each their own
    singleton group — the zero-interaction default, identical to today's
    flat per-camera iteration order.

    Keying blank values by (True, idx) rather than a sentinel string makes
    collision with a user-typed group label impossible.
    """
    groups_by_key: dict = {}
    order = []
    for idx in indices:
        raw = (group_of(idx) or "").strip()
        key = (True, idx) if not raw else (False, raw)
        if key not in groups_by_key:
            groups_by_key[key] = []
            order.append(key)
        groups_by_key[key].append(idx)
    return [groups_by_key[key] for key in order]


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
                         lens_info: Optional[dict] = None,
                         shared_dir: Optional[str] = None,
                         file_stem: Optional[str] = None) -> list:
    """Grab `count` frames and save them.

    CLI / default path (no `shared_dir` / `file_stem`): writes into
    `<outdir>/<model>_<serial>/` as
    `<model>_<serial>_<shot:04d>_<timestamp>.<fmt>`.

    GUI session path: pass `shared_dir` (the dated shot folder) and
    `file_stem` (e.g. NorthCam) so files land as
    `{file_stem}_{shot:03d}.{fmt}` inside that shared folder — no
    per-camera subfolder, no timestamp in the filename.

    `lens_info`, if given, is {"mm", "brand", "model"} session documentation
    appended via append_manifest_entry (legacy; GUI session capture writes
    its own shot-level manifest instead). Returns the list of filenames
    saved (basename only).
    """
    serial = camera.DeviceInfo.GetSerialNumber()
    model = sanitize(camera.DeviceInfo.GetModelName())
    if shared_dir:
        cam_dir = shared_dir
    else:
        cam_dir = os.path.join(outdir, f"{model}_{serial}")
    os.makedirs(cam_dir, exist_ok=True)
    saved: list = []

    try:
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

        camera.StartGrabbingMax(count)
        shot = 0
        warned_bit_depth = False
        while camera.IsGrabbing():
            try:
                with camera.RetrieveResult(
                    RETRIEVE_TIMEOUT_MS, pylon.TimeoutHandling_ThrowException
                ) as grab_result:
                    if not grab_result.GrabSucceeded():
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

                    shot += 1
                    if file_stem:
                        filename = f"{file_stem}_{shot:03d}.{fmt}"
                    else:
                        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                        filename = f"{model}_{serial}_{shot:04d}_{ts}.{fmt}"
                    converted.Save(FILE_FORMATS[fmt], os.path.join(cam_dir, filename))
                    saved.append(filename)
                    if progress_cb:
                        progress_cb(shot, count)
            except pylon.TimeoutException as e:
                LOG.error("%s: grab timeout: %s", serial, e)
                break
        log_camera_state(camera, "post-grab")
        if lens_info is not None:
            append_manifest_entry(cam_dir, {
                "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                "serial": serial, "model": model,
                "count": count, "format": fmt,
                "lens_mm": lens_info.get("mm"), "lens_brand": lens_info.get("brand"),
                "lens_model": lens_info.get("model"),
                "synchronized": False, "group_label": None,
            })
    finally:
        if camera.IsGrabbing():
            camera.StopGrabbing()
        if camera.IsOpen():
            camera.Close()
    return saved


# ---------------------------------------------------------------------------
# Hardware-synchronized group capture (GUI-only; a camera Group of size >= 2)
#
# Uses IEEE 1588 PTP clock sync + GigE Vision Scheduled Action Commands to
# fire FrameStart on every camera in a group at (as close to) the same
# instant. This is the one deliberate, narrowly-scoped exception to the
# script's "never writes acquisition parameters" rule — and only for
# cameras a human explicitly placed in a shared Group via the GUI; a camera
# left in its own group never has any of this code run against it.
#
# Node names below (PtpEnable vs. GevIEEE1588, GevIEEE1588Status,
# PtpServoStatus, GevIEEE1588OffsetFromMaster, ActionDeviceKey/GroupKey/
# GroupMask, TriggerSource="Action1", etc.) are sourced from Basler's
# official docs and pypylon's own source/tests, not guessed.
# ---------------------------------------------------------------------------

class HardwareSyncError(RuntimeError):
    """Raised internally; always caught at the top level of
    capture_group_synchronized and turned into a per-camera error string —
    never propagates to the caller."""


class PtpConvergenceError(HardwareSyncError):
    pass


class ActionCommandError(HardwareSyncError):
    pass


PTP_ENABLE_NODE_CANDIDATES = ("PtpEnable", "GevIEEE1588")           # a2A vs. acA
PTP_LATCH_NODE_CANDIDATES = ("PtpDataSetLatch", "GevIEEE1588DataSetLatch")
PTP_STATUS_NODE_CANDIDATES = ("PtpStatus", "GevIEEE1588Status")     # a2A vs. acA — confirmed
                                                                      # against real hardware to
                                                                      # split the same way as the
                                                                      # enable node, contrary to
                                                                      # this session's earlier
                                                                      # (wrong) research summary
TIMESTAMP_LATCH_NODE_CANDIDATES = ("TimestampLatch", "GevTimestampControlLatch")
TIMESTAMP_VALUE_NODE_CANDIDATES = ("TimestampLatchValue", "GevTimestampValue")
PTP_OFFSET_NODE_CANDIDATES = ("PtpOffsetFromMaster", "GevIEEE1588OffsetFromMaster")  # a2A vs. acA

PTP_OFFSET_THRESHOLD_NS = 1_000_000         # 1 ms
OFFSET_WINDOW_SAMPLES = 5                    # MAX over this many polls — not monotonic
PTP_CONVERGENCE_TIMEOUT_S = 90.0             # Basler: "a few seconds or minutes" — no fixed sleep
PTP_POLL_INTERVAL_S = 0.5

ACTION_DEVICE_KEY = 1
ACTION_GROUP_KEY = 1
ACTION_GROUP_MASK = pylon.AllGroupMask       # 0xffffffff
ACTION_TIME_MARGIN_NS = 500_000_000          # 500 ms; doubled on _ActionLate retry, capped
ACTION_TIME_MARGIN_CAP_NS = 4_000_000_000
ACTION_TIME_MARGIN_FLOOR_NS = 100_000_000    # never shrink below this
ACTION_TIME_MARGIN_SHRINK_AFTER = 3          # consecutive clean rounds before shrinking
ACTION_TIME_MARGIN_SHRINK_FACTOR = 0.7
SCHEDULED_ACTION_ISSUE_TIMEOUT_MS = 2000

# --- Reliability/throughput tuning for grouped (simultaneous) capture only —
# the solo/sequential path has no simultaneous-burst bandwidth contention, so
# it's deliberately left untouched. Confirmed against real hardware (pypylon
# 26.7, emulated device): MaxNumBuffer defaults to 10 and is genuinely
# settable pre-grab. AutoPacketSize lives on the STREAM GRABBER node map
# (camera.GetStreamGrabberNodeMap(), not the device node map, and not via a
# GetStreamGrabberParams() method — that attribute exists but is a
# non-callable PlaceholderParameter in this pypylon version) — confirmed
# absent entirely on the emulator's stream grabber (architecturally
# expected: packet-size negotiation is meaningless without real network
# transport), so this could only be probe-tested, not proven, without real
# GigE hardware — verify it's actually available on real cameras before
# trusting it silently no-ops. GevSCBWA (bandwidth assigned) is a device-node
# reading, also unavailable on the emulator for the same reason.
GROUP_MIN_NUM_BUFFER = 16
GIGE_LINK_BANDWIDTH_BYTES_PER_SEC = 125_000_000   # ~125 MB/s per 1GigE link
                                                    # (Basler AW00144501000)
BANDWIDTH_WARNING_FRACTION = 0.85


def probe_node(camera, names):
    """First candidate that exists AND is available on this camera —
    distinct from read_node()'s IsReadable(), which is about a specific
    reading, not 'does this camera line even expose this feature.'

    Confirmed against real hardware: there is no IsAvailable() *method* on
    pypylon's typed parameter objects (BooleanParameter/EnumParameter/etc.)
    — that's the C++ GenApi::IsAvailable(node) free function's Python
    binding, exposed as genicam.IsAvailable(node), not node.IsAvailable().
    Calling the (nonexistent) method silently AttributeErrors, which an
    earlier version of this function swallowed via a bare except — making
    every real PTP node look "not found" even when it was fully available.
    """
    for n in names:
        node = getattr(camera, n, None)
        if node is not None:
            try:
                if genicam.IsAvailable(node):
                    return n, node
            except Exception:
                continue
    return None, None


# --- Trigger-state snapshot / restore ("never leave a camera stuck") -------
#
# pypylon's InstantCamera registers CAcquireContinuousConfiguration by
# default on Open(), which forces TriggerMode=Off/AcquisitionMode=Continuous
# — so the snapshot taken right after Open() reflects that default, not
# necessarily whatever was configured in Pylon Viewer before. That's fine:
# TriggerMode=Off is exactly the safe restore target, and it's what every
# existing run of this script already leaves a camera in.

def _snapshot_trigger_state(camera) -> dict:
    return {
        "TriggerSelector": read_node(camera.TriggerSelector),
        "TriggerMode": read_node(camera.TriggerMode),
        "TriggerSource": read_node(camera.TriggerSource),
    }


def _restore_trigger_state(camera, state: dict, serial: str) -> None:
    # Runs in a finally block — must never raise.
    try:
        for name in ("TriggerSelector", "TriggerMode", "TriggerSource"):
            val = state.get(name)
            if val is not None:
                getattr(camera, name).TrySetValue(val)
    except Exception:
        LOG.exception("%s: failed to restore trigger state", serial)


def _configure_action_trigger(camera, serial: str) -> None:
    camera.TriggerSelector.SetValue("FrameStart")
    camera.TriggerMode.SetValue("On")
    camera.TriggerSource.SetValue("Action1")
    camera.ActionDeviceKey.SetValue(ACTION_DEVICE_KEY)
    camera.ActionGroupKey.SetValue(ACTION_GROUP_KEY)
    camera.ActionGroupMask.SetValue(ACTION_GROUP_MASK)
    # Written directly (not via pylon.ActionTriggerConfiguration, whose
    # TriggerSelector coverage was never confirmed) — read back to verify
    # our own writes actually stuck before trusting them.
    if read_node(camera.TriggerMode) != "On" or read_node(camera.TriggerSource) != "Action1":
        raise ActionCommandError(f"{serial}: trigger config did not take effect")


def _configure_group_streaming(camera, serial: str, log_cb=None) -> None:
    """Best-effort reliability tuning for one camera in a synchronized group,
    called once per camera right before StartGrabbingMax(). Never raises —
    a failure here should degrade to "less robust against bandwidth
    contention," not abort an otherwise-working capture. Neither setting
    needs restoring afterward: both are host-side StreamGrabber session
    parameters, not persisted to the camera, so a fresh Open() next time
    reverts to pylon's own defaults on its own.
    """
    try:
        if genicam.IsAvailable(camera.MaxNumBuffer) and camera.MaxNumBuffer.GetValue() < GROUP_MIN_NUM_BUFFER:
            camera.MaxNumBuffer.SetValue(GROUP_MIN_NUM_BUFFER)
    except Exception:
        LOG.exception("%s: could not raise MaxNumBuffer", serial)

    try:
        sg_nodemap = camera.GetStreamGrabberNodeMap()
        auto_packet_size = sg_nodemap.GetNode("AutoPacketSize")
        if auto_packet_size is not None and genicam.IsAvailable(auto_packet_size):
            auto_packet_size.SetValue(True)
        elif log_cb:
            log_cb(f"{serial}: AutoPacketSize not available on this camera/transport — "
                   "skipping (only expected on real GigE hardware, not the emulator)")
    except Exception:
        LOG.exception("%s: could not enable AutoPacketSize", serial)


def _check_group_bandwidth(cameras, serials, log_cb=None) -> None:
    """Non-blocking diagnostic: sums each camera's current GevSCBWA (actual
    bandwidth demand at its current settings) and warns via log_cb if the
    group's combined demand looks tight against one GigE link's ~125MB/s
    budget. This is exactly the Basler-documented mechanism (AW00144501000)
    for predicting the bandwidth-contention failure mode this whole feature
    is trying to make more robust against — a warning, not a hard gate,
    since the math is a heuristic (doesn't account for GevSCPD/GevSCFTD
    tuning that might already mitigate it) and GevSCBWA may simply be
    unavailable (e.g. on the emulator — never blocks capture either way).
    """
    total = 0
    readable = {}
    for cam, serial in zip(cameras, serials):
        try:
            node = getattr(cam, "GevSCBWA", None)
            if node is not None and genicam.IsAvailable(node):
                val = read_node(node)
                if val is not None:
                    readable[serial] = val
                    total += val
        except Exception:
            LOG.exception("%s: could not read GevSCBWA", serial)

    if not readable:
        return  # nothing readable (e.g. emulator) — no basis for a warning
    threshold = GIGE_LINK_BANDWIDTH_BYTES_PER_SEC * BANDWIDTH_WARNING_FRACTION
    if total > threshold and log_cb:
        log_cb(
            f"bandwidth warning: this group's combined GevSCBWA is "
            f"{total / 1e6:.1f} MB/s ({readable}) — exceeds "
            f"{BANDWIDTH_WARNING_FRACTION:.0%} of one GigE link's ~125MB/s budget. "
            "If cameras share one switch/NIC, this can cause buffer underruns "
            "during synchronized capture (see README's Ubuntu network tuning notes)."
        )


# --- PTP enable + shared-deadline convergence poll --------------------------
#
# Enable PTP on every camera first (fast), then poll all cameras together
# against one shared deadline — polling one camera to completion before even
# enabling the next would let it converge against a partial view of the PTP
# domain.

def _enable_ptp(camera, serial: str) -> None:
    name, node = probe_node(camera, PTP_ENABLE_NODE_CANDIDATES)
    if node is None:
        raise PtpConvergenceError(f"{serial}: no PTP-enable node found")
    if not node.TrySetValue(True):
        raise PtpConvergenceError(f"{serial}: camera rejected enabling {name}")


def _wait_group_ptp_converged(cameras, serials, log_cb=None,
                               timeout_s=PTP_CONVERGENCE_TIMEOUT_S,
                               poll_interval_s=PTP_POLL_INTERVAL_S) -> dict:
    """Returns {serial: last_observed_status} once every camera reaches a
    converged state (Master, or Slave/Uncalibrated with lock confirmed).
    Raises PtpConvergenceError on timeout or a Faulty status."""
    per_cam = {}
    for cam, serial in zip(cameras, serials):
        _, status_node = probe_node(cam, PTP_STATUS_NODE_CANDIDATES)
        if status_node is None:
            raise PtpConvergenceError(f"{serial}: no PTP status node found")
        per_cam[serial] = {
            "latch": probe_node(cam, PTP_LATCH_NODE_CANDIDATES)[1],
            "status": status_node,
            "servo": probe_node(cam, ("PtpServoStatus",))[1],
            "offset": probe_node(cam, PTP_OFFSET_NODE_CANDIDATES)[1],
            "window": [], "last_status": None,
        }

    pending = set(serials)
    deadline = time.monotonic() + timeout_s
    while pending and time.monotonic() < deadline:
        for serial in list(pending):
            st = per_cam[serial]
            if st["latch"] is not None:
                st["latch"].Execute()
            status = read_node(st["status"])
            st["last_status"] = status

            if status == "Faulty":
                raise PtpConvergenceError(f"{serial}: PTP status is Faulty")
            if status in (None, "Initializing", "Listening", "Pre_Master"):
                continue
            if status == "Master":
                pending.discard(serial)           # terminal, converged state
                continue
            # Slave/Uncalibrated: check actual lock quality.
            if st["servo"] is not None:
                if read_node(st["servo"]) == "Locked":
                    pending.discard(serial)
            elif st["offset"] is not None:
                off = read_node(st["offset"])
                if off is not None:
                    w = st["window"]
                    w.append(abs(off))
                    del w[:-OFFSET_WINDOW_SAMPLES]
                    if len(w) >= OFFSET_WINDOW_SAMPLES and max(w) < PTP_OFFSET_THRESHOLD_NS:
                        pending.discard(serial)
            elif status == "Slave":
                pending.discard(serial)           # weakest fallback: no servo/offset node at all

        if log_cb:
            log_cb(f"PTP convergence: waiting on {sorted(pending) or 'none — converged'}")
        if pending:
            time.sleep(poll_interval_s)

    if pending:
        raise PtpConvergenceError(
            f"PTP did not converge within {timeout_s}s for: {sorted(pending)}"
        )
    return {s: per_cam[s]["last_status"] for s in serials}


def _select_reference_clock(statuses: dict) -> str:
    """Pick the elected grandmaster (status == 'Master') as the camera whose
    latched timestamp action_time_ns is computed from. Also doubles as the
    split-domain check: >1 Master means a broken L2 segment."""
    masters = [s for s, v in statuses.items() if v == "Master"]
    if len(masters) > 1:
        raise PtpConvergenceError(
            f"split PTP domain — multiple masters {masters} (statuses={statuses}); "
            "confirm all grouped cameras are on the same L2 segment/switch"
        )
    if not masters:
        raise PtpConvergenceError(f"no camera reached Master status (statuses={statuses})")
    return masters[0]


CLOCK_AGREEMENT_TOLERANCE_NS = 500_000_000  # 500 ms — generous on purpose; see below.


def _read_camera_timestamp_ns(camera, serial: str) -> int:
    _, latch_node = probe_node(camera, TIMESTAMP_LATCH_NODE_CANDIDATES)
    _, value_node = probe_node(camera, TIMESTAMP_VALUE_NODE_CANDIDATES)
    if latch_node is None or value_node is None:
        raise HardwareSyncError(f"{serial}: no timestamp-latch node found")
    latch_node.Execute()
    return value_node.GetValue()


def _verify_clock_agreement(cameras, serials) -> None:
    """Sanity-check, once per group right after PTP convergence, that the
    TimestampLatch/Value nodes we'll actually schedule against agree with
    each other across every camera in the group.

    Confirmed against real hardware (a2A4200/a2A4504/acA4112): once PTP is
    enabled and converged, TimestampLatchValue/GevTimestampValue tick at a
    genuine real-time nanosecond rate and agree within single-digit
    milliseconds across cameras — but they are NOT Unix-epoch nanoseconds.
    Per the GigE Vision spec, this node is device-relative (reset at
    power-up/reset), not wall-clock-based; PTP disciplines its rate and
    cross-device agreement, not its zero point. An earlier version of this
    function checked `timestamp >= 10**17` as a proxy for "is this real PTP
    time" — that assumption was wrong and would reject every legitimate
    reading from real Basler hardware. The check that actually matches the
    risk being guarded against (scheduling against a camera whose
    TimestampLatch isn't truly disciplined by the same PTP domain, despite
    its status/offset nodes self-reporting convergence) is cross-camera
    agreement, checked here directly.
    """
    readings = {serial: _read_camera_timestamp_ns(cam, serial) for cam, serial in zip(cameras, serials)}
    spread = max(readings.values()) - min(readings.values())
    if spread > CLOCK_AGREEMENT_TOLERANCE_NS:
        raise HardwareSyncError(
            f"grouped cameras' timestamps disagree by {spread / 1e6:.1f}ms "
            f"(readings={readings}) — exceeds {CLOCK_AGREEMENT_TOLERANCE_NS / 1e6:.0f}ms "
            "tolerance; refusing to schedule against an inconsistent clock"
        )


def _next_action_time_ns(ref_camera, ref_serial: str, margin_ns: int) -> int:
    now_ns = _read_camera_timestamp_ns(ref_camera, ref_serial)
    return now_ns + margin_ns


# --- Per-shot round loop (respects the acA's 1-deep action queue) ----------
#
# pypylon's grab engine fills its own buffer queue in the background once
# StartGrabbingMax is active — the concurrency need isn't "poll N cameras in
# parallel to avoid dropped frames," it's "never let a slow/timed-out
# RetrieveResult cause scheduling round k+1 while round k might still be
# outstanding on the acA (1-deep queue, no ActionQueueSize node at all)."

def _make_capture_ctx(camera, serial: str, fmt: str, outdir: str,
                       shared_dir: Optional[str] = None) -> dict:
    """`shared_dir`, if given, is used as this camera's output directory
    instead of its own `<model>_<serial>` subfolder — how a Group's cameras
    end up saving into one shared folder together."""
    model = sanitize(camera.DeviceInfo.GetModelName())
    cam_dir = shared_dir if shared_dir else os.path.join(outdir, f"{model}_{serial}")
    os.makedirs(cam_dir, exist_ok=True)
    return {
        "camera": camera, "serial": serial, "model": model, "cam_dir": cam_dir,
        "converter": build_converter(camera), "fmt": fmt, "shot_counter": 0,
    }


def _retrieve_and_save_one(ctx: dict, timeout_ms: int):
    cam = ctx["camera"]
    try:
        with cam.RetrieveResult(timeout_ms, pylon.TimeoutHandling_ThrowException) as gr:
            if not gr.GrabSucceeded():
                return ("grab_error", f"{gr.ErrorCode:#x} {gr.ErrorDescription}")
            converted = ctx["converter"].Convert(gr)
            ctx["shot_counter"] += 1
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filename = f"{ctx['model']}_{ctx['serial']}_{ctx['shot_counter']:04d}_{ts}.{ctx['fmt']}"
            converted.Save(FILE_FORMATS[ctx["fmt"]], os.path.join(ctx["cam_dir"], filename))
            return ("ok", filename)
    except pylon.TimeoutException as e:
        return ("timeout", str(e))


def _issue_action_and_collect_round(executor, ctxs, gige_tl, action_time_ns,
                                     timeout_ms, expected_ok_count, broadcast_address):
    # Submit RetrieveResult futures BEFORE the broadcast so every camera is
    # already blocked-and-waiting when the hardware trigger actually fires.
    futures = {ctx["serial"]: executor.submit(_retrieve_and_save_one, ctx, timeout_ms) for ctx in ctxs}
    ok, raw_results = gige_tl.IssueScheduledActionCommandWait(
        ACTION_DEVICE_KEY, ACTION_GROUP_KEY, ACTION_GROUP_MASK,
        action_time_ns, broadcast_address, SCHEDULED_ACTION_ISSUE_TIMEOUT_MS, expected_ok_count,
    )
    # raw_results' list-to-device ordering was never confirmed against real
    # hardware — log it as an aggregate signal only. Per-camera outcomes
    # come entirely from RetrieveResult, which is unambiguous.
    outcomes = {serial: f.result() for serial, f in futures.items()}
    return ok, raw_results, outcomes


def capture_group_synchronized(cameras, serials, count, fmt, outdir,
                                progress_cb=None, log_cb=None,
                                broadcast_address="255.255.255.255",
                                group_label: str = "",
                                lens_info: Optional[dict] = None) -> dict:
    """Hardware-synchronized capture for one Group's cameras (size >= 2).
    Never raises to the caller — catches HardwareSyncError internally and
    records it per-camera, matching the existing per-camera try/except
    pattern used elsewhere in this script (e.g. run_cli's capture loop).
    Returns {serial: {"shots_saved": int, "error": Optional[str]}}.

    `group_label` (the raw Group field text) names the ONE shared output
    directory every camera in this group saves into — this is what makes a
    synchronized shot set land together instead of split across per-camera
    subfolders. `lens_info`, if given, is {serial: {"mm", "brand", "model"}}
    — session documentation only (see append_manifest_entry), never written
    to any camera.
    """
    results = {s: {"shots_saved": 0, "error": None} for s in serials}
    ctxs, gige_tl, snapshots, shared_dir = [], None, {}, None
    try:
        # 1. Early GigE gate (before any Open()) — the whole mechanism is
        #    GigE Vision Action Commands; fail fast on a USB3/emulated camera
        #    rather than deep inside PTP convergence after wasted opens.
        for cam in cameras:
            device_class = cam.GetDeviceInfo().GetDeviceClass()
            if device_class != "BaslerGigE":
                raise HardwareSyncError(
                    "all grouped cameras must be GigE for hardware sync "
                    f"(found {device_class})"
                )
        # 2. Open, snapshot, build per-camera output contexts — all sharing
        #    one directory named after this group's label.
        shared_dir = os.path.join(outdir, f"group_{sanitize(group_label)}") if group_label else outdir
        os.makedirs(shared_dir, exist_ok=True)
        for cam, serial in zip(cameras, serials):
            cam.Open()
            snapshots[serial] = _snapshot_trigger_state(cam)
            ctxs.append(_make_capture_ctx(cam, serial, fmt, outdir, shared_dir=shared_dir))
        # 3. Enable + converge PTP, then discover the elected reference clock.
        for cam, serial in zip(cameras, serials):
            _enable_ptp(cam, serial)
        statuses = _wait_group_ptp_converged(cameras, serials, log_cb=log_cb)
        ref_serial = _select_reference_clock(statuses)
        ref_camera = dict(zip(serials, cameras))[ref_serial]
        _verify_clock_agreement(cameras, serials)
        _check_group_bandwidth(cameras, serials, log_cb=log_cb)
        # 4. Configure Action-Command triggering + reliability tuning, arm
        #    grabbing on every camera.
        for cam, serial in zip(cameras, serials):
            _configure_action_trigger(cam, serial)
            _configure_group_streaming(cam, serial, log_cb=log_cb)
            cam.StartGrabbingMax(count)
        # 5. Per-shot round loop: one broadcast Action Command per shot,
        #    waiting for every camera's frame before advancing.
        gige_tl = pylon.TlFactory.GetInstance().CreateTl(pylon.BaslerGigEDeviceClass)
        with ThreadPoolExecutor(max_workers=len(cameras)) as executor:
            margin_ns = ACTION_TIME_MARGIN_NS
            clean_rounds = 0   # consecutive rounds with no _ActionLate retry and no drop
            shot_idx = 1
            while shot_idx <= count:
                action_time_ns = _next_action_time_ns(ref_camera, ref_serial, margin_ns)
                ok, raw, outcomes = _issue_action_and_collect_round(
                    executor, ctxs, gige_tl, action_time_ns,
                    RETRIEVE_TIMEOUT_MS, len(cameras), broadcast_address,
                )
                if log_cb:
                    log_cb(f"shot {shot_idx}/{count}: command_ok={ok} raw={raw} outcomes={outcomes} "
                           f"margin_ns={margin_ns}")
                if not ok:
                    if "_ActionLate" in str(raw) and margin_ns < ACTION_TIME_MARGIN_CAP_NS:
                        margin_ns = min(margin_ns * 2, ACTION_TIME_MARGIN_CAP_NS)
                        clean_rounds = 0
                        continue   # retry same shot_idx with a bigger margin
                    for s in serials:
                        results[s]["error"] = results[s]["error"] or (
                            f"shot {shot_idx}: action command not accepted ({raw})"
                        )
                    break
                failed = {s: v for s, v in outcomes.items() if v[0] != "ok"}
                for s, (status, info) in outcomes.items():
                    if status == "ok":
                        results[s]["shots_saved"] += 1
                        if progress_cb:
                            progress_cb(s, shot_idx, count)
                    else:
                        results[s]["error"] = f"shot {shot_idx}: {status} ({info})"
                if failed:
                    # A dropped frame means we can't be sure the acA's
                    # single-deep action queue is clear — continuing risks
                    # _Overflow or misattributing a late frame to the wrong
                    # shot index. Abort remaining shots; already-saved shots
                    # for every camera stay on disk.
                    if log_cb:
                        log_cb(f"aborting remaining shots after shot {shot_idx}: {failed}")
                    break
                # Every camera in this round reported "ok" with no
                # _ActionLate retry needed — a genuinely clean round. After
                # enough of these in a row, the margin is provably more
                # generous than this network/hardware actually needs, so
                # shrink it (bounded by a floor) to cut per-shot overhead on
                # long runs. Any future _ActionLate immediately grows it back.
                clean_rounds += 1
                if clean_rounds >= ACTION_TIME_MARGIN_SHRINK_AFTER and margin_ns > ACTION_TIME_MARGIN_FLOOR_NS:
                    margin_ns = max(int(margin_ns * ACTION_TIME_MARGIN_SHRINK_FACTOR), ACTION_TIME_MARGIN_FLOOR_NS)
                    clean_rounds = 0
                shot_idx += 1
    except HardwareSyncError as e:
        for s in serials:
            if results[s]["error"] is None:
                results[s]["error"] = str(e)
    finally:
        for ctx in ctxs:
            cam, serial = ctx["camera"], ctx["serial"]
            try:
                if cam.IsGrabbing():
                    cam.StopGrabbing()
            except Exception:
                LOG.exception("%s: StopGrabbing failed", serial)
            if serial in snapshots:
                _restore_trigger_state(cam, snapshots[serial], serial)
            try:
                if cam.IsOpen():
                    cam.Close()
            except Exception:
                LOG.exception("%s: Close failed", serial)
        if gige_tl is not None:
            pylon.TlFactory.GetInstance().ReleaseTl(gige_tl)
    if lens_info is not None and ctxs:
        manifest_dir = shared_dir or outdir
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        for ctx in ctxs:
            serial, model = ctx["serial"], ctx["model"]
            info = lens_info.get(serial) or {}
            append_manifest_entry(manifest_dir, {
                "timestamp": ts, "serial": serial, "model": model,
                "count": count, "format": fmt,
                "lens_mm": info.get("mm"), "lens_brand": info.get("brand"),
                "lens_model": info.get("model"),
                "synchronized": True, "group_label": group_label or None,
                "shots_saved": results[serial]["shots_saved"],
                "error": results[serial]["error"],
            })
    return results


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
    except ValueError as e:
        print(f"ERROR: {e}")
        return 1

    for idx in indices:
        print(f"Capturing {count} image(s) from {devices[idx].GetModelName()} "
              f"(S/N {devices[idx].GetSerialNumber()})...")
        try:
            camera = pylon.InstantCamera(tl_factory.CreateDevice(devices[idx]))
            capture_from_camera(camera, count, fmt, outdir)
        except Exception as e:
            LOG.error("Failed capturing from %s: %s", devices[idx].GetSerialNumber(), e)
            print(f"  ERROR: {e}")

    print("Done.")
    return 0


# ---------------------------------------------------------------------------
# Optional Tkinter GUI: session capture (plate/bolts, BMP, dated shot folders)
# ---------------------------------------------------------------------------
# Visual shell is Dori-branded (navy header, teal primary, gray page, white
# rounded cards with pill controls). GUI capture is sequential into a shared
# dated shot folder — no Group / PTP / Apply Settings path. CLI is unchanged.

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
        with open(path, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2)
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
            canvas.lower()

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

        def draw(_event=None):
            cnv.delete("all")
            if kind == "solid":
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

        cnv.bind("<Enter>", lambda _e: (hover.__setitem__(0, True), draw()))
        cnv.bind("<Leave>", lambda _e: (hover.__setitem__(0, False), draw()))
        cnv.bind("<Button-1>", lambda _e: command())
        draw()
        return cnv

    status_var = tk.StringVar(
        value="Ready." if devices else "No cameras detected. Connect a camera and click Rescan."
    )
    ui_queue: "queue.Queue" = queue.Queue()
    capturing = [False]  # list-boxed so nested functions can mutate it without `nonlocal`

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
             "<folder>/<MM-DD>/<HHMMSS>/ with NorthCam / SouthCam / TopCam BMPs.",
        bg=DORI_BG, fg=DORI_MUTED, font=(family, 10), anchor="w", justify="left",
    ).pack(side="left", fill="x", expand=True)

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
    def worker(indices, count, outdir, plate_color, bolts_size):
        now = datetime.datetime.now()
        date_folder = now.strftime("%m-%d")
        time_folder = now.strftime("%H%M%S")
        shot_folder = os.path.join(outdir, date_folder, time_folder)
        os.makedirs(shot_folder, exist_ok=True)
        ui_queue.put(f"Shot folder: {shot_folder}")

        fmt = "bmp"
        cameras_meta = []
        for idx in indices:
            d = devices[idx]
            serial = str(d.GetSerialNumber())
            model_raw = d.GetModelName()
            model = sanitize(model_raw)
            alias = camera_alias(serial)
            stem = camera_file_stem(serial, model_raw)
            label = alias or model_raw

            def cb(shot, total, label=label):
                ui_queue.put(f"{label}: {shot}/{total}")

            files = []
            try:
                cam = pylon.InstantCamera(tl_factory.CreateDevice(d))
                files = capture_from_camera(
                    cam, count, fmt, outdir, progress_cb=cb,
                    shared_dir=shot_folder, file_stem=stem,
                )
            except Exception as e:
                ui_queue.put(f"{label}: ERROR {e}")

            cameras_meta.append({
                "serial": serial,
                "model": model_raw,
                "alias": alias or stem,
                "files": files,
            })

        write_shot_manifest(shot_folder, {
            "plate_color": plate_color,
            "bolts_size": bolts_size,
            "timestamp": now.isoformat(timespec="seconds"),
            "shot_folder": shot_folder,
            "cameras": cameras_meta,
            "count": count,
            "format": fmt,
        })
        ui_queue.put("__DONE__")

    def poll_queue():
        try:
            while True:
                msg = ui_queue.get_nowait()
                if msg == "__DONE__":
                    capturing[0] = False
                    status_var.set("Capture complete.")
                else:
                    status_var.set(msg)
        except queue.Empty:
            pass
        root.after(100, poll_queue)

    def start_capture():
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

        main_outdir = outdir_var.get().strip() or os.path.abspath("./captures")
        outdir_var.set(main_outdir)
        os.makedirs(main_outdir, exist_ok=True)
        capturing[0] = True
        threading.Thread(
            target=worker,
            args=(
                indices, count, main_outdir,
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
        if devices:
            status_var.set(f"Found {len(devices)} camera(s).")
        else:
            status_var.set("No cameras detected. Connect a camera and click Rescan.")

    bar, bar_inner = _rounded_panel(
        root, fill=DORI_CARD, radius=16, shadow=True, canvas_bg=DORI_BG,
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
    _pill_button(
        row2, "Capture", start_capture, kind="solid", canvas_bg=DORI_CARD,
    ).pack(side="right")
    _pill_button(
        row2, "Rescan", rescan, kind="outline", canvas_bg=DORI_CARD,
    ).pack(side="right", padx=(0, 10))

    status_row = tk.Frame(root, bg=DORI_BG)
    status_row.pack(fill="x", padx=28, pady=(0, 12))
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
