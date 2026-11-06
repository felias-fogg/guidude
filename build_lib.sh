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
# Environment (all optional):
#   PYTHON                     Python to build the module for (default: python3).
#                              Must be the Python that runs GUIDude / PyInstaller.
#   DEPS_PREFIX                prefix of libusb, hidapi, libftdi (macOS default:
#                              Homebrew prefix; Linux: system paths)
#   MACOSX_DEPLOYMENT_TARGET   oldest macOS version to support
#   AVRDUDE_CMAKE_ARGS         extra arguments for cmake
#
# Requirements: cmake, swig >= 4, flex, bison, the Python headers, plus the
# usual avrdude deps (libusb, hidapi, libftdi1; optionally libelf).

set -e
here=$(cd "$(dirname "$0")" && pwd)
src=${1:-"$here/../avrdude"}
src=$(cd "$src" && pwd)
ostype=$(uname | tr '[A-Z]' '[a-z]')
bdir="$src/build_$ostype"

if [ ! -f "$src/CMakeLists.txt" ]; then
    echo "No avrdude source tree at $src" >&2
    exit 1
fi

py=$(command -v "${PYTHON:-python3}")
prefix=${DEPS_PREFIX:-}
if [ -z "$prefix" ] && [ "$ostype" = darwin ]; then
    prefix=$(brew --prefix 2>/dev/null || true)
fi

# avrdude finds some headers (e.g. libusb.h) only via CMAKE_C_FLAGS
set -- -D CMAKE_BUILD_TYPE=RelWithDebInfo -D "Python3_EXECUTABLE=$py"
if [ -n "$prefix" ]; then
    set -- "$@" -D "CMAKE_PREFIX_PATH=$prefix" -D "CMAKE_C_FLAGS=-I$prefix/include" \
        -D "CMAKE_EXE_LINKER_FLAGS=-L$prefix/lib" -D "CMAKE_MODULE_LINKER_FLAGS=-L$prefix/lib" \
        -D "CMAKE_SHARED_LINKER_FLAGS=-L$prefix/lib"
fi
if [ "$ostype" = darwin ]; then
    # one architecture only, even with a universal2 Python
    set -- "$@" -D "CMAKE_OSX_ARCHITECTURES=$(uname -m)"
    [ -n "$MACOSX_DEPLOYMENT_TARGET" ] && \
        set -- "$@" -D "CMAKE_OSX_DEPLOYMENT_TARGET=$MACOSX_DEPLOYMENT_TARGET"
fi

echo "== Building avrdude in $bdir for $py${prefix:+ (deps: $prefix)}"
# shellcheck disable=SC2086
cmake -S "$src" -B "$bdir" "$@" $AVRDUDE_CMAKE_ARGS
cmake --build "$bdir" -j 8

b="$bdir/src"
if [ ! -f "$b/swig_avrdude.py" ]; then
    echo "SWIG module was not built. Is swig installed and were the Python" >&2
    echo "headers found? (see 'Python support' in the cmake output)" >&2
    exit 1
fi

mkdir -p "$here/lib"
rm -f "$here"/lib/_swig_avrdude*
cp "$b"/_swig_avrdude*.so "$b"/swig_avrdude.py "$b"/avrdude.conf "$here/lib/"
echo "== Copied libavrdude Python module and avrdude.conf to $here/lib"
ls -l "$here/lib"
