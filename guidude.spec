# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for GUIDude.
#
#   ./build_lib.sh                 # once: puts libavrdude module + avrdude.conf into lib/
#   pyinstaller guidude.spec       # -> dist/GUIDude.app (macOS) or dist/GUIDude/ (Linux/Windows)
#
# The SWIG module (swig_avrdude.py + _swig_avrdude.so/.pyd) is found through
# `pathex`, so PyInstaller analyses it like any other import and also collects
# the shared libraries it links against (libusb, hidapi, libftdi, ...).

import re
import sys
from pathlib import Path

here = Path(SPECPATH)
# single source of truth for the version: APP_VERSION in guidude.py
version = re.search(r'^APP_VERSION = "([^"]+)"', (here / "guidude.py").read_text(encoding="utf-8"), re.M)[1]
lib = here / "lib"
if not (lib / "swig_avrdude.py").is_file():
    raise SystemExit("lib/swig_avrdude.py missing - run ./build_lib.sh first")

a = Analysis(
    [str(here / "guidude.py")],
    pathex=[str(lib)],
    binaries=[],
    datas=[(str(lib / "avrdude.conf"), ".")],
    hiddenimports=["swig_avrdude", "_swig_avrdude"],
    excludes=["numpy", "PIL", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GUIDude",
    console=False,
    debug=False,
    strip=False,
    upx=False,
    argv_emulation=False,
    # Linux/Windows: the folder next to the executable with Python and all
    # libraries (PyInstaller's default "_internal" says little when the two
    # are copied elsewhere); inside a macOS .app the name does not matter
    contents_directory="GUIDude_internal" if sys.platform != "darwin" else "_internal",
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="GUIDude")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="GUIDude.app",
        bundle_identifier="org.nongnu.avrdude.guidude",
        info_plist={
            "CFBundleShortVersionString": version,
            "CFBundleVersion": version,
            "NSHighResolutionCapable": True,
        },
    )
