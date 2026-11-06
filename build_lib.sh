#!/bin/sh
# Build avrdude with the SWIG/Python binding and copy the three runtime
# files GUIDude needs into ./lib:
#
#   _swig_avrdude.so   (or .pyd on Windows)  - libavrdude as Python extension
#   swig_avrdude.py                           - SWIG proxy module
#   avrdude.conf                              - part and programmer database
#
# Usage: ./build_lib.sh [path/to/avrdude]     (default: ../avrdude)
#
# Requirements: cmake, swig >= 4, Python 3 headers of the *same* Python
# that will run GUIDude, plus the usual avrdude deps (libusb, hidapi,
# libftdi1, optionally libelf for ELF file support).

set -e
here=$(cd "$(dirname "$0")" && pwd)
src=${1:-"$here/../avrdude"}
src=$(cd "$src" && pwd)
ostype=$(uname | tr '[A-Z]' '[a-z]')

if [ ! -f "$src/build.sh" ]; then
    echo "No avrdude source tree at $src" >&2
    exit 1
fi

echo "== Building avrdude in $src/build_$ostype"
(cd "$src" && ./build.sh -j 8)

b="$src/build_$ostype/src"
if [ ! -f "$b/swig_avrdude.py" ]; then
    echo "SWIG module was not built. Is swig installed and were the Python" >&2
    echo "headers found? (see 'Python support' in the cmake output)" >&2
    exit 1
fi

mkdir -p "$here/lib"
cp "$b"/_swig_avrdude*.so "$b"/swig_avrdude.py "$b"/avrdude.conf "$here/lib/"
echo "== Copied libavrdude Python module and avrdude.conf to $here/lib"
ls -l "$here/lib"
