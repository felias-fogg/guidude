#!/bin/sh
# Build a self-contained GUIDude with PyInstaller.
#   ./make_dist.sh            -> dist/GUIDude.app (macOS) or dist/GUIDude/ (Linux)
# Run ./build_lib.sh first (once) to produce lib/.
set -e
cd "$(dirname "$0")"
[ -f lib/swig_avrdude.py ] || ./build_lib.sh
# same Python as the one the SWIG module was built for (see build_lib.sh)
if [ ! -x .venv/bin/pyinstaller ]; then
    "${PYTHON:-python3}" -m venv .venv
    .venv/bin/pip install -q "pyinstaller${PYINSTALLER_VERSION:+==$PYINSTALLER_VERSION}"
fi
.venv/bin/pyinstaller --noconfirm guidude.spec
if [ -d dist/GUIDude.app ]; then
    exe=dist/GUIDude.app/Contents/MacOS/GUIDude
else
    exe=dist/GUIDude/GUIDude
fi
"$exe" --selftest
echo "== Done: $(ls -d dist/GUIDude.app 2>/dev/null || echo dist/GUIDude)"
