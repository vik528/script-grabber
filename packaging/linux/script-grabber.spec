# -*- mode: python ; coding: utf-8 -*-
#
# Custom spec (not the plain `pyinstaller --onefile` invocation) because of a
# confirmed real bug affecting camera discovery inside the frozen bundle.
#
# Root cause, confirmed by direct inspection of the Analysis TOC lists (not
# guessed): a.binaries already correctly destines every one of these
# libraries under 'pypylon/' — there is no duplication there at all. The
# actual problem is in a.datas: PyInstaller additionally creates a
# top-level SYMLINK entry for each of them (e.g. dest='libpylonbase.so.12'
# -> src='pypylon/libpylonbase.so.12'). PyInstaller's bootloader puts the
# bundle's top-level directory first on LD_LIBRARY_PATH, so the dynamic
# linker resolves the bare library name to that top-level SYMLINK. Even
# though the symlink points at the same underlying file, libpylonbase's own
# self-location logic (dladdr()-style: "what path was I opened as") sees
# the SYMLINK's path, not its target's — so dirname() of that reports the
# top-level directory, where none of the GigE/USB3/emulator transport-layer
# .so files actually live (they're one level down, under pypylon/). Hence
# "globbing failed" and zero cameras ever found, even PYLON_CAMEMU-emulated
# ones. Confirmed by direct testing: removing these top-level symlinks (so
# LD_LIBRARY_PATH search fails at top level and falls through via RUNPATH
# to the real pypylon/-nested file directly, with no symlink indirection)
# fixes device discovery completely.
#
# The fix here: filter these specific top-level symlink entries out of
# Analysis.datas before packaging — a.binaries is left untouched, since it
# was never the source of the duplication.

a = Analysis(
    ['capture_cameras.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

_DUP_BASLER_LIBS = {
    'libpylonbase.so.12',
    'libpylonutility.so.12',
    'libGenApi_gcc_v3_5_Basler_pylon_v1.so',
    'libGCBase_gcc_v3_5_Basler_pylon_v1.so',
    'libLog_gcc_v3_5_Basler_pylon_v1.so',
    'libMathParser_gcc_v3_5_Basler_pylon_v1.so',
    'libXmlParser_gcc_v3_5_Basler_pylon_v1.so',
    'libNodeMapData_gcc_v3_5_Basler_pylon_v1.so',
    'libPylonDataProcessingCore.so.7',
}
# entry[0] is the destination path within the bundle — an exact match (no
# directory prefix) means it's the unwanted top-level symlink; the correct
# copy has a dest path like 'pypylon/libpylonbase.so.12' and is left
# untouched.
_before = len(a.datas)
a.datas = [entry for entry in a.datas if entry[0] not in _DUP_BASLER_LIBS]
_removed = _before - len(a.datas)
print(f"[script-grabber.spec] removed {_removed} top-level Basler library symlink "
      f"entries out of {len(_DUP_BASLER_LIBS)} known names (a.binaries left untouched — "
      f"it already correctly destines these libraries under pypylon/, no duplication there)")

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='script-grabber',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
