# Builds a standalone Linux executable of capture_cameras.py via PyInstaller.
#
# Pinned to ubuntu:20.04 deliberately: pypylon's own Linux wheels are
# manylinux_2_31 (glibc >= 2.31), and PyInstaller's own guidance is to build
# on the OLDEST glibc you need to support — Ubuntu 20.04 ships glibc 2.31,
# which is that floor. Building here maximizes forward compatibility with
# newer Ubuntu versions; it does NOT support Ubuntu 18.04 or older, since
# pypylon itself doesn't.
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
FROM --platform=${BUILD_PLATFORM} ubuntu:20.04

ENV DEBIAN_FRONTEND=noninteractive

# Ubuntu 20.04's DEFAULT python3 is 3.8 — but pypylon's modern releases
# (confirmed: the 26.x line) only publish wheels tagged cp39-abi3 (Python
# 3.9+, stable ABI). Under Python 3.8, pip silently falls back to the
# newest pypylon release that still has a cp38 wheel — which turned out to
# be a very old 3.0.1, missing APIs this script depends on (confirmed:
# AttributeError on pylon.ImageFileFormat_Bmp). Fix: compile Python 3.11
# from source, directly against this same Ubuntu 20.04 base (tk-dev/tcl-dev
# installed first so Tkinter is built in) — this was chosen over the
# deadsnakes PPA after the PPA's package index failed to resolve any
# packages in this container despite fetching successfully (flaky/blocked
# in this environment); compiling from source has no such external
# dependency and keeps the resulting executable linked only against this
# base's glibc 2.31, preserving the broad backward compatibility this
# Dockerfile is pinned to 20.04 for.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        wget \
        binutils \
        ca-certificates \
        zlib1g-dev \
        libssl-dev \
        libbz2-dev \
        libreadline-dev \
        libsqlite3-dev \
        libncursesw5-dev \
        libffi-dev \
        liblzma-dev \
        tk-dev \
        tcl-dev \
    && rm -rf /var/lib/apt/lists/*

RUN wget -q https://www.python.org/ftp/python/3.11.9/Python-3.11.9.tgz \
    && tar -xzf Python-3.11.9.tgz \
    && cd Python-3.11.9 \
    && ./configure --prefix=/usr/local --enable-shared \
    && make -j"$(nproc)" \
    && make altinstall \
    && cd .. && rm -rf Python-3.11.9 Python-3.11.9.tgz \
    && echo "/usr/local/lib" > /etc/ld.so.conf.d/python3.11.conf \
    && ldconfig

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

RUN python3.11 -m venv /build/venv \
    && /build/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /build/venv/bin/pip install --no-cache-dir \
        "pypylon==${PYPYLON_VERSION}" \
        "pyinstaller==${PYINSTALLER_VERSION}" \
        "pyinstaller-hooks-contrib==${PYINSTALLER_HOOKS_CONTRIB_VERSION}" \
    && /build/venv/bin/python -c "import tkinter" # fail fast here, not deep in the PyInstaller run, if Tk wasn't actually built in

# Uses the custom script-grabber.spec (not a plain `--onefile` invocation) —
# it strips top-level symlinks PyInstaller creates for pypylon's core Basler
# libraries, which would otherwise break camera discovery entirely inside
# the frozen bundle (confirmed by direct testing). See the spec file's own
# header comment for the full explanation.
RUN /build/venv/bin/pyinstaller script-grabber.spec

# Emulator-based smoke test (no real cameras, no display available in this
# container — CLI path only; the GUI and real-hardware discovery get
# verified separately on an actual Ubuntu machine, see Phase 2 of the plan).
RUN PYLON_CAMEMU=2 ./dist/script-grabber --cameras all --count 2 --format tiff --outdir /tmp/smoke_test_out \
    && ls -la /tmp/smoke_test_out
