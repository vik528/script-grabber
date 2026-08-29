#!/usr/bin/env python3
"""
capture_cameras.py — Multi-camera capture utility for Basler cameras
(GigE / USB3, via pypylon / the Pylon SDK).

Auto-detects every Basler camera pypylon can see, lets you pick which ones to
use, and captures a chosen number of images from each. Camera settings
(exposure, gain, etc.) are left exactly as configured in Pylon Viewer — this
script never writes acquisition parameters in its default (CLI) flow. Run
with --gui for an optional window that also lets you view/adjust exposure
and gain before capturing, and group cameras for hardware-synchronized
capture (see README.md's "Camera groups" note).

Usage:
    python3 capture_cameras.py
    python3 capture_cameras.py --cameras all --count 10 --format tiff --outdir ./captures
    python3 capture_cameras.py --gui

Testing without hardware: set PYLON_CAMEMU=2 (or more) before running to
have pypylon emulate that many virtual cameras.
"""
from __future__ import annotations

__version__ = "1.1.0"

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

# GigE diagnostics (GUI-only, read-only) — see read_gige_diagnostics().
GIGE_PACKET_SIZE_NODE_NAME = "GevSCPSPacketSize"                    # device node map
GIGE_RESEND_COUNT_NODE_NAME = "Statistic_Resend_Packet_Count"       # StreamGrabberNodeMap,
                                                                      # same map AutoPacketSize
                                                                      # already lives on
# (node name, display label) — tried in order; falls back to a differently
# labeled bandwidth metric rather than silently mislabeling it, since
# Basler's own docs don't confirm DeviceLinkSpeed is exposed on all their
# GigE cameras (SFNC-standard node; presence not verified against real
# Basler hardware yet).
GIGE_LINK_SPEED_NODE_CANDIDATES = (
    ("DeviceLinkSpeed", "Link Speed"),
    ("GevSCBWA", "Bandwidth Assigned"),
)


def sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


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


def read_gige_diagnostics(camera, serial: str) -> dict:
    """Best-effort, read-only GigE diagnostics snapshot for one already-open
    camera: current packet size, cumulative resend-packet count, and link
    speed (or a differently-labeled bandwidth fallback — see
    GIGE_LINK_SPEED_NODE_CANDIDATES). GUI-only (Rescan / the "Show GigE
    Diagnostics" toggle) — never called from the CLI path or during a
    capture. Values are "n/a" for anything this camera doesn't expose
    (expected for USB3/emulated devices) — never raises.

    UNCONFIRMED, needs a real-hardware check before trusting blindly (same
    bar as AutoPacketSize/GevSCBWA got): (1) whether
    Statistic_Resend_Packet_Count persists across separate Open()/Close()
    cycles, so a later on-demand read still reflects an earlier capture's
    actual resend activity, or resets to 0 on every fresh Open() — if it
    resets, this diagnostic is only meaningful read *within* the same open
    session as the capture being diagnosed, not from a later Rescan/toggle
    click; (2) the assumed Bytes/sec unit for DeviceLinkSpeed (inferred from
    its sibling GevSCBWA/GevSCDMT nodes' confirmed Bytes/sec convention, not
    independently confirmed for DeviceLinkSpeed itself).
    """
    result = {"packet_size": "n/a", "resend_count": "n/a",
              "link_label": "Link Speed", "link_value": "n/a"}

    node = getattr(camera, GIGE_PACKET_SIZE_NODE_NAME, None)
    if node is not None:
        try:
            if genicam.IsAvailable(node):
                val = read_node(node)
                if val is not None:
                    result["packet_size"] = f"{val} B"
        except Exception:
            LOG.exception("%s: could not read %s", serial, GIGE_PACKET_SIZE_NODE_NAME)

    try:
        sg_nodemap = camera.GetStreamGrabberNodeMap()
        resend_node = sg_nodemap.GetNode(GIGE_RESEND_COUNT_NODE_NAME)
        if resend_node is not None and genicam.IsAvailable(resend_node):
            val = read_node(resend_node)
            if val is not None:
                result["resend_count"] = str(val)
    except Exception:
        LOG.exception("%s: could not read %s", serial, GIGE_RESEND_COUNT_NODE_NAME)

    for name, label in GIGE_LINK_SPEED_NODE_CANDIDATES:
        node = getattr(camera, name, None)
        if node is None:
            continue
        try:
            if not genicam.IsAvailable(node):
                continue
            val = read_node(node)
            if val is None:
                continue
            result["link_label"] = label
            result["link_value"] = f"{val / 1e6:.1f} MB/s"
            break
        except Exception:
            LOG.exception("%s: could not read %s", serial, name)
    return result


# ---------------------------------------------------------------------------
# Core per-camera grab-and-save loop (shared by CLI and GUI)
# ---------------------------------------------------------------------------

def capture_from_camera(camera, count: int, fmt: str, outdir: str, progress_cb=None,
                         lens_info: Optional[dict] = None) -> None:
    """`lens_info`, if given, is {"mm": str|None, "brand": str|None,
    "model": str|None} — pure session documentation (GUI-only; CLI callers
    never pass this), recorded in a manifest file, never written to the
    camera or read back by this script."""
    serial = camera.DeviceInfo.GetSerialNumber()
    model = sanitize(camera.DeviceInfo.GetModelName())
    cam_dir = os.path.join(outdir, f"{model}_{serial}")
    os.makedirs(cam_dir, exist_ok=True)

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
                    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                    filename = f"{model}_{serial}_{shot:04d}_{ts}.{fmt}"
                    converted.Save(FILE_FORMATS[fmt], os.path.join(cam_dir, filename))
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
# Optional Tkinter GUI: camera selection + exposure/gain + capture, in one place
# ---------------------------------------------------------------------------

def run_gui() -> int:
    import tkinter as tk
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
    root.title(f"Basler Multi-Camera Capture v{__version__}")
    status_var = tk.StringVar(
        value="Ready." if devices else "No cameras detected. Connect a camera and click Rescan."
    )
    ui_queue: "queue.Queue" = queue.Queue()
    capturing = [False]  # list-boxed so nested functions can mutate it without `nonlocal`

    cam_vars = []
    exposure_vars = []
    group_vars = []
    lens_mm_vars = []
    lens_brand_vars = []
    lens_model_vars = []
    diag_vars = []
    diag_row_widgets = []
    show_diag_var = tk.BooleanVar(value=False)

    cams_frame = ttk.Frame(root)
    cams_frame.pack(fill="x", padx=8, pady=8)

    def copy_group_to_selected(source_idx):
        # Reads group_vars/cam_vars by reference — always current, including
        # right after a Rescan rebuilds both lists in place.
        value = group_vars[source_idx].get()
        targets = [i for i, v in enumerate(cam_vars) if v.get()]
        if not targets:
            messagebox.showwarning(
                "No cameras selected",
                "Check at least one camera to copy the Group value to.",
            )
            return
        for i in targets:
            group_vars[i].set(value)
        status_var.set(f"Copied Group '{value}' to {len(targets)} camera(s).")

    def build_camera_rows():
        # Rebuildable so Rescan can pick up cameras connected after the
        # window was already opened, without restarting the GUI. Mutates
        # devices/cam_vars/exposure_vars/group_vars/lens_*_vars/diag_vars IN
        # PLACE (clear + re-populate) rather than rebinding them, so every
        # other closure in this function (worker/start_capture/
        # apply_settings/load_current_settings/refresh_gige_diagnostics) —
        # all defined once, reading these same list objects by reference —
        # sees the rebuilt contents automatically.
        for child in cams_frame.winfo_children():
            child.destroy()
        cam_vars.clear()
        exposure_vars.clear()
        group_vars.clear()
        lens_mm_vars.clear()
        lens_brand_vars.clear()
        lens_model_vars.clear()
        diag_vars.clear()
        diag_row_widgets.clear()

        if not devices:
            ttk.Label(
                cams_frame,
                text="No cameras detected. Connect a camera and click Rescan.",
            ).pack(anchor="w", padx=4, pady=4)
            return

        for i, d in enumerate(devices):
            block = ttk.Frame(cams_frame)
            block.pack(fill="x", pady=(2, 6))

            row = ttk.Frame(block)
            row.pack(fill="x")
            v = tk.BooleanVar(value=True)
            cam_vars.append(v)
            ttk.Checkbutton(row, text=f"{d.GetModelName()} (S/N {d.GetSerialNumber()})",
                             variable=v).pack(side="left")

            exp_var = tk.StringVar()
            exposure_vars.append(exp_var)
            ttk.Label(row, text="Exposure (us):").pack(side="left", padx=(12, 0))
            ttk.Entry(row, textvariable=exp_var, width=10).pack(side="left")

            # Blank by default — collision-free (no shared implicit label
            # like str(i) could accidentally pull two rows into the same
            # group) and means "capture on its own," identical to today's
            # behavior with zero user interaction.
            group_var = tk.StringVar()
            group_vars.append(group_var)
            ttk.Label(row, text="Group:").pack(side="left", padx=(8, 0))
            ttk.Entry(row, textvariable=group_var, width=14).pack(side="left")
            ttk.Button(
                row, text="Copy", width=6,
                command=lambda i=i: copy_group_to_selected(i),
            ).pack(side="left", padx=(4, 0))

            # Second, indented sub-row: lens info — pure session
            # documentation, never written to the camera. Kept on its own
            # line rather than appended to `row` so the camera list stays
            # scannable rather than growing into one very wide row per
            # camera.
            lens_row = ttk.Frame(block)
            lens_row.pack(fill="x", padx=(24, 0), pady=(1, 0))
            lens_mm_var = tk.StringVar()
            lens_brand_var = tk.StringVar()
            lens_model_var = tk.StringVar()
            lens_mm_vars.append(lens_mm_var)
            lens_brand_vars.append(lens_brand_var)
            lens_model_vars.append(lens_model_var)
            ttk.Label(lens_row, text="Lens (mm):").pack(side="left")
            ttk.Entry(lens_row, textvariable=lens_mm_var, width=6).pack(side="left")
            ttk.Label(lens_row, text="Brand:").pack(side="left", padx=(8, 0))
            ttk.Entry(lens_row, textvariable=lens_brand_var, width=10).pack(side="left")
            ttk.Label(lens_row, text="Model (optional):").pack(side="left", padx=(8, 0))
            ttk.Entry(lens_row, textvariable=lens_model_var, width=14).pack(side="left")

            # Third, indented sub-row: GigE diagnostics — hidden unless
            # "Show GigE Diagnostics" is checked (see that Checkbutton
            # below). Created every rebuild either way so the toggle can
            # show/hide it without a rescan; visibility here at build time
            # just matches whatever the toggle's current state already is.
            diag_var = tk.StringVar(value="GigE diagnostics: not read yet.")
            diag_vars.append(diag_var)
            diag_row = ttk.Frame(block)
            ttk.Label(diag_row, textvariable=diag_var).pack(side="left")
            diag_row_widgets.append(diag_row)
            if show_diag_var.get():
                diag_row.pack(fill="x", padx=(24, 0), pady=(1, 0))

    build_camera_rows()

    ttk.Label(
        root,
        text="Group: cameras sharing the same Group value fire together "
             "(hardware-synced) and save into one shared folder. Leave blank "
             "to capture that camera on its own, as today.",
    ).pack(fill="x", padx=8, pady=(0, 4))
    ttk.Label(
        root,
        text="Lens (mm)/Brand/Model: session documentation only, saved "
             "alongside the images — never written to the camera. All three "
             "fields may be left blank.",
    ).pack(fill="x", padx=8, pady=(0, 4))
    ttk.Checkbutton(
        root,
        text="Show GigE Diagnostics (packet size / resend count / link speed) "
             "— opens each camera briefly to read; check after a capture to "
             "look for dropped-packet signs over a switch",
        variable=show_diag_var,
        command=lambda: on_toggle_diagnostics(),
    ).pack(anchor="w", padx=8, pady=(0, 4))

    def _read_first(cam, names, default=""):
        for n in names:
            val = read_node(getattr(cam, n))
            if val is not None:
                return val
        return default

    def _write_first(cam, names, value) -> bool:
        for n in names:
            if getattr(cam, n).TrySetValue(value):
                return True
        return False

    def _create_camera_or_report(d):
        # CreateDevice() itself can raise for a camera that's on the network
        # but currently unreachable/flaky (confirmed live: a real GigE camera
        # failed here with a RuntimeException reading device memory) — must
        # be guarded exactly like Open(), not just assumed to succeed.
        try:
            return pylon.InstantCamera(tl_factory.CreateDevice(d))
        except Exception as e:
            status_var.set(f"Camera {d.GetSerialNumber()}: could not connect ({e}).")
            return None

    def _open_or_report(cam, serial) -> bool:
        try:
            cam.Open()
            return True
        except Exception as e:
            status_var.set(
                f"Camera {serial}: could not open ({e}). Close Pylon Viewer / "
                "any other connection to it first."
            )
            return False

    def load_current_settings():
        # Read-only — never writes anything, safe to call on window open.
        for i, d in enumerate(devices):
            cam = _create_camera_or_report(d)
            if cam is None:
                continue
            try:
                if not _open_or_report(cam, d.GetSerialNumber()):
                    continue
                exposure_vars[i].set(str(_read_first(cam, EXPOSURE_NODE_CANDIDATES)))
            finally:
                if cam.IsOpen():
                    cam.Close()

    def apply_settings():
        for i, d in enumerate(devices):
            if not cam_vars[i].get():
                continue
            cam = _create_camera_or_report(d)
            if cam is None:
                continue
            try:
                if not _open_or_report(cam, d.GetSerialNumber()):
                    continue
                # Auto-exposure must be off before a manual write will take
                # effect — only disabled here, on explicit submit.
                cam.ExposureAuto.TrySetValue("Off")
                if exposure_vars[i].get():
                    if not _write_first(cam, EXPOSURE_NODE_CANDIDATES, float(exposure_vars[i].get())):
                        status_var.set(f"Camera {i}: exposure value rejected by device.")
            finally:
                if cam.IsOpen():
                    cam.Close()
        status_var.set("Settings applied.")

    def refresh_gige_diagnostics():
        # On-demand snapshot (open -> read -> close), like
        # load_current_settings() — never holds a camera open continuously,
        # so it can't block Capture from acquiring it. See
        # read_gige_diagnostics()'s docstring for what "on-demand" means for
        # the resend-count reading specifically (unconfirmed against real
        # hardware whether it's still meaningful this way).
        for i, d in enumerate(devices):
            cam = _create_camera_or_report(d)
            if cam is None:
                diag_vars[i].set("GigE diagnostics: could not connect.")
                continue
            try:
                if not _open_or_report(cam, d.GetSerialNumber()):
                    diag_vars[i].set("GigE diagnostics: could not open camera.")
                    continue
                diag = read_gige_diagnostics(cam, d.GetSerialNumber())
                diag_vars[i].set(
                    f"Packet Size: {diag['packet_size']}   "
                    f"Resends: {diag['resend_count']}   "
                    f"{diag['link_label']}: {diag['link_value']}"
                )
            finally:
                if cam.IsOpen():
                    cam.Close()
        status_var.set("GigE diagnostics refreshed.")

    def on_toggle_diagnostics():
        show = show_diag_var.get()
        for w in diag_row_widgets:
            if show:
                w.pack(fill="x", padx=(24, 0), pady=(1, 0))
            else:
                w.pack_forget()
        if show:
            refresh_gige_diagnostics()

    bottom = ttk.Frame(root)
    bottom.pack(fill="x", padx=8, pady=(0, 8))
    count_var = tk.IntVar(value=10)
    fmt_var = tk.StringVar(value="tiff")
    outdir_var = tk.StringVar(value=os.path.abspath("./captures"))

    ttk.Label(bottom, text="Count:").pack(side="left")
    ttk.Entry(bottom, textvariable=count_var, width=6).pack(side="left")
    ttk.Label(bottom, text="Format:").pack(side="left", padx=(8, 0))
    ttk.OptionMenu(bottom, fmt_var, "tiff", "tiff", "png", "bmp").pack(side="left")
    ttk.Button(
        bottom, text="Browse...",
        command=lambda: outdir_var.set(filedialog.askdirectory() or outdir_var.get()),
    ).pack(side="left", padx=(8, 0))

    ttk.Label(root, textvariable=status_var).pack(fill="x", padx=8, pady=(0, 4))

    # Capture runs in a worker thread; Tk widgets must only ever be touched
    # from the main thread, so the worker pushes status strings onto a queue
    # that a root.after loop drains on the GUI thread.
    def worker(groups, count, fmt, outdir, lens_by_idx):
        for group in groups:
            if len(group) == 1:
                idx = group[0]
                label = devices[idx].GetModelName()

                def cb(shot, total, label=label):
                    ui_queue.put(f"{label}: {shot}/{total}")

                try:
                    cam = pylon.InstantCamera(tl_factory.CreateDevice(devices[idx]))
                    capture_from_camera(cam, count, fmt, outdir, progress_cb=cb,
                                         lens_info=lens_by_idx.get(idx))
                except Exception as e:
                    ui_queue.put(f"{label}: ERROR {e}")
            else:
                serials = [devices[i].GetSerialNumber() for i in group]
                group_desc = "+".join(devices[i].GetModelName() for i in group)
                group_label = group_vars[group[0]].get().strip()
                lens_info = {devices[i].GetSerialNumber(): lens_by_idx.get(i) for i in group}
                ui_queue.put(f"[Group {group_desc}] starting synchronized capture...")
                try:
                    cams = [pylon.InstantCamera(tl_factory.CreateDevice(devices[i])) for i in group]
                    results = capture_group_synchronized(
                        cams, serials, count, fmt, outdir,
                        progress_cb=lambda s, shot, total: ui_queue.put(f"{s}: {shot}/{total} (sync)"),
                        log_cb=ui_queue.put,
                        group_label=group_label, lens_info=lens_info,
                    )
                    for s, r in results.items():
                        tail = f" — {r['error']}" if r["error"] else " — OK"
                        ui_queue.put(f"{s}: {r['shots_saved']}/{count} shots{tail}")
                except Exception as e:
                    # Safety net: CreateDevice() above is outside
                    # capture_group_synchronized's own try/except, so an
                    # unexpected failure there still needs to surface here
                    # instead of silently killing the worker thread.
                    ui_queue.put(f"[Group {group_desc}] ERROR {e}")
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

        lens_by_idx = {}
        for i in indices:
            mm_raw = lens_mm_vars[i].get().strip()
            mm = None
            if mm_raw:
                try:
                    mm = float(mm_raw)
                    if mm <= 0:
                        raise ValueError
                except ValueError:
                    messagebox.showwarning(
                        "Invalid lens (mm)",
                        f"Camera {devices[i].GetModelName()}: Lens (mm) must be a "
                        "positive number, or left blank.",
                    )
                    return
            lens_by_idx[i] = {
                "mm": mm,
                "brand": lens_brand_vars[i].get().strip() or None,
                "model": lens_model_vars[i].get().strip() or None,
            }

        os.makedirs(outdir_var.get(), exist_ok=True)
        groups = partition_into_groups(indices, lambda i: group_vars[i].get())
        capturing[0] = True
        threading.Thread(
            target=worker, args=(groups, count, fmt_var.get(), outdir_var.get(), lens_by_idx),
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
            load_current_settings()
            status_var.set(f"Found {len(devices)} camera(s).")
            if show_diag_var.get():
                refresh_gige_diagnostics()  # overwrites the status line above with its own
        else:
            status_var.set("No cameras detected. Connect a camera and click Rescan.")

    ttk.Button(bottom, text="Rescan", command=rescan).pack(side="left", padx=(8, 0))
    ttk.Button(bottom, text="Apply Settings", command=apply_settings).pack(side="left", padx=(8, 0))
    ttk.Button(bottom, text="Capture", command=start_capture).pack(side="left", padx=(8, 0))

    load_current_settings()
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
