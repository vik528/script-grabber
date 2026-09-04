# Builds a standalone Linux executable of capture_cameras.py via PyInstaller.
#
# Pinned to ubuntu:24.04 deliberately: that is the oldest Ubuntu this tool
# is deployed on (lab floor). PyInstaller's guidance is still to build on
# the OLDEST glibc you need to support — Ubuntu 24.04 ships glibc 2.39, so
# the resulting binary requires glibc >= 2.39 (24.04+). Do not rebuild on
# 20.04/22.04 hoping for broader reach; production targets are 24.04 only.
#
# Ubuntu 24.04's stock python3 is 3.12, which is new enough for pypylon's
# cp39-abi3 wheels — no need to compile Python from source (that existed
# only because 20.04's default Python was 3.8).
#
# Usage (from the Script Grabber/ directory):
#   docker build -f packaging/linux/Dockerfile -t script-grabber-build .
#   docker create --name sg-extract script-grabber-build
#   docker cp sg-extract:/build/dist/script-grabber ./script-grabber-linux
#   docker rm sg-extract
#
# Platform is pinned to linux/amd64 explicitly (not left to follow the build
# host's default architecture) — real deployment/lab machines are expected
# to be standard x86_64 Ubuntu PCs, not ARM. Building this on an Apple
# Silicon Mac would otherwise silently produce an arm64 Linux binary that
# won't run on the actual x86_64 target machines. Docker Desktop emulates
# amd64 on ARM hosts via Rosetta/QEMU to make this pin work.
ARG BUILD_PLATFORM=linux/amd64
FROM --platform=${BUILD_PLATFORM} ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# libpython3.12 is required by PyInstaller onefile when using the distro
# Python (it looks up libpython3.12.so.1.0 at Analysis time). Without it:
# "Python shared library ('libpython3.12.so.1.0') was not found!"
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        python3-tk \
        python3-pip \
        libpython3.12 \
        binutils \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY capture_cameras.py .
COPY packaging/linux/script-grabber.spec .

# Pinned to the exact versions this build was actually developed and
# real-hardware-tested against — deliberately NOT left to "whatever's
# latest on PyPI today," which would make the build silently drift over
# time (and could reintroduce a bug like the earlier symlink issue or a
# newer/older API surface this script wasn't written against). Bump these
# together, deliberately, re-running the full smoke test + real-hardware
# checklist, rather than letting them float.
ARG PYPYLON_VERSION=26.7
ARG PYINSTALLER_VERSION=6.22.2
ARG PYINSTALLER_HOOKS_CONTRIB_VERSION=2026.7
# Pillow: GUI JPEG post-capture preview frames (preview_<Alias>.jpg + strip).
ARG PILLOW_VERSION=11.2.1

RUN python3 -m venv /build/venv \
    && /build/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /build/venv/bin/pip install --no-cache-dir \
        "pypylon==${PYPYLON_VERSION}" \
        "pyinstaller==${PYINSTALLER_VERSION}" \
        "pyinstaller-hooks-contrib==${PYINSTALLER_HOOKS_CONTRIB_VERSION}" \
        "Pillow==${PILLOW_VERSION}" \
    && /build/venv/bin/python -c "import tkinter" # fail fast here, not deep in the PyInstaller run, if python3-tk wasn't installed

# Uses the custom script-grabber.spec (not a plain `--onefile` invocation) —
# it strips top-level symlinks PyInstaller creates for pypylon's core Basler
# libraries, which would otherwise break camera discovery entirely inside
# the frozen bundle (confirmed by direct testing). See the spec file's own
# header comment for the full explanation.
RUN /build/venv/bin/pyinstaller script-grabber.spec

# Emulator-based smoke test (no real cameras, no display available in this
# container — CLI path only; the GUI and real-hardware discovery get
# verified separately on an actual Ubuntu machine).
RUN PYLON_CAMEMU=2 ./dist/script-grabber --cameras all --count 2 --format tiff --outdir /tmp/smoke_test_out \
    && ls -la /tmp/smoke_test_out
