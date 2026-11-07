#!/bin/sh
# Build a self-contained GUIDude with PyInstaller.
#   ./make_dist.sh            -> dist/GUIDude.app (macOS) or dist/GUIDude/ (Linux,
#                                Windows: run in Git Bash)
# Run ./build_lib.sh first (once) to produce lib/.
set -e
cd "$(dirname "$0")"
[ -f lib/swig_avrdude.py ] || ./build_lib.sh
# same Python as the one the SWIG module was built for (see build_lib.sh)
bin=.venv/bin
[ -d .venv/Scripts ] && bin=.venv/Scripts      # Windows venv layout
if [ ! -x $bin/pyinstaller ] && [ ! -x $bin/pyinstaller.exe ]; then
    "${PYTHON:-python3}" -m venv .venv
    [ -d .venv/Scripts ] && bin=.venv/Scripts
    $bin/python -m pip install -q --upgrade pip      # old distro pips are too old
    $bin/python -m pip install -q "pyinstaller${PYINSTALLER_VERSION:+==$PYINSTALLER_VERSION}"
fi
$bin/pyinstaller --noconfirm guidude.spec
if [ -d dist/GUIDude.app ]; then
    exe=dist/GUIDude.app/Contents/MacOS/GUIDude
elif [ -f dist/GUIDude/GUIDude.exe ]; then
    exe=dist/GUIDude/GUIDude.exe
else
    exe=dist/GUIDude/GUIDude
fi
"$exe" --selftest
echo "== Done: $(ls -d dist/GUIDude.app 2>/dev/null || echo dist/GUIDude)"
