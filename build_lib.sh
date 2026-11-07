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
# On Windows run it in Git Bash with Visual Studio (Build Tools) 2022, CMake,
# SWIG and winflexbison installed: libusb, hidapi, libftdi and libelf are then
# fetched by avrdude's cmake (USE_EXTERNAL_LIBS) and linked statically.
#
# Environment (all optional):
#   PYTHON                     Python to build the module for (default: python3).
#                              Must be the Python that runs GUIDude / PyInstaller.
#   DEPS_PREFIX                prefix of libusb, hidapi, libftdi (macOS default:
#                              Homebrew prefix; Linux: system paths)
#   MACOSX_DEPLOYMENT_TARGET   oldest macOS version to support
#   AVRDUDE_CMAKE_ARGS         extra arguments for cmake
#
# Requirements: cmake, swig >= 4, flex, bison, git, the Python headers, plus
# the usual avrdude deps (libusb, hidapi, libftdi1; optionally libelf).
#
# The patches in avrdude-patches/ are applied to the avrdude source tree.

set -e
here=$(cd "$(dirname "$0")" && pwd)
src=${1:-"$here/../avrdude"}
src=$(cd "$src" && pwd)
ostype=$(uname | tr '[A-Z]' '[a-z]')
case $ostype in mingw*|msys*|cygwin*) ostype=windows ;; esac
bdir="$src/build_$ostype"

if [ ! -f "$src/CMakeLists.txt" ]; then
    echo "No avrdude source tree at $src" >&2
    exit 1
fi

# Fixes for libavrdude (submitted upstream) from avrdude-patches/:
#  0001  reset the serial transport and timeouts per session (else serial
#        programmers fail after a session with a USB debugger or urclock)
#  0002  make partdesc/pgmid settable from Python (urclock needs partdesc)
#  0003  Python binding: release the serial port when open() fails (else it
#        stays busy, on Windows until GUIDude is restarted)
#  0004  Python binding: do not link libpython (else the module works only with
#        the Python it was built with); -D PYTHON_LIMITED_API=3.10 in
#        AVRDUDE_CMAKE_ARGS builds one abi3 module for Python 3.10 and later
#  0005  PICkit 5/Basic: real data transfers in MSVC builds (were stubs)
# On Windows, avrdude-patches/libusb/ holds fixes for avrdude's libusb-win32
# (avrdudes/libusb, fetched by cmake): it is cloned at the commit avrdude
# pins, patched and used instead (PICkit 5/Basic: WinUSB on an interface).
# Each patch is applied unless the source already contains it.
for p in "$here"/avrdude-patches/*.patch; do
    [ -f "$p" ] || continue
    # avrdude's sources use LF everywhere: drop CRs a Windows checkout of the
    # patches may have added (.gitattributes should prevent that)
    lf() { tr -d '\r' < "$p"; }
    if lf | git -C "$src" apply --reverse --check - 2>/dev/null; then
        echo "== Already in avrdude source: $(basename "$p")"
    elif lf | git -C "$src" apply --check - 2>/dev/null; then
        lf | git -C "$src" apply -
        echo "== Applied to avrdude source: $(basename "$p")"
    else
        echo "== WARNING: $(basename "$p") does not apply to $src - check whether" \
             "this avrdude has an equivalent fix" >&2
        # release builds must not silently go without a fix
        if [ "$CI" = true ]; then exit 1; fi
    fi
done

py=$(command -v "${PYTHON:-python3}")
if [ "$ostype" = windows ]; then
    # Git Bash would turn MSVC options like /MT into paths: switch that off
    # and give cmake Windows paths ourselves
    export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL="*"
    py=$(cygpath -m "$py")
    src=$(cygpath -m "$src")
    bdir=$(cygpath -m "$bdir")
fi
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

if [ "$ostype" = windows ] && ls "$here"/avrdude-patches/libusb/*.patch > /dev/null 2>&1; then
    tag=$(sed -n '/FetchContent_Declare(libusb/,/)/s/.*GIT_TAG *\([0-9a-f]*\).*/\1/p' "$src/CMakeLists.txt")
    lsrc="$bdir/libusb-patched"
    rm -rf "$lsrc"      # small; a fresh clone keeps LF line endings for the patches
    git -c core.autocrlf=false clone -q https://github.com/avrdudes/libusb.git "$lsrc"
    git -C "$lsrc" checkout -q "${tag:-HEAD}"
    for p in "$here"/avrdude-patches/libusb/*.patch; do
        # the checkout may have given the patch CRLF line endings (autocrlf)
        tr -d '\r' < "$p" | git -C "$lsrc" apply -
        echo "== Applied to libusb-win32 (${tag:-HEAD}): $(basename "$p")"
    done
    set -- "$@" -D "FETCHCONTENT_SOURCE_DIR_LIBUSB=$lsrc"
fi

if [ "$ostype" = windows ]; then
    # as in avrdude's own MSVC build: static runtime, bundled USB libraries
    set -- "$@" -A x64 -D USE_EXTERNAL_LIBS=1 \
        -D HAVE_LIBREADLINE=HAVE_LIBREADLINE-NOTFOUND \
        -D "CMAKE_C_FLAGS_RELWITHDEBINFO=/MT /GL /Zi /O2 /Ob1 /DNDEBUG" \
        -D "CMAKE_CXX_FLAGS_RELWITHDEBINFO=/MT /GL /Zi /O2 /Ob1 /DNDEBUG" \
        -D "CMAKE_SHARED_LINKER_FLAGS_RELWITHDEBINFO=/DEBUG /INCREMENTAL:NO /LTCG /OPT:REF /OPT:ICF" \
        -D "CMAKE_MODULE_LINKER_FLAGS_RELWITHDEBINFO=/DEBUG /INCREMENTAL:NO /LTCG /OPT:REF /OPT:ICF" \
        -D "CMAKE_EXE_LINKER_FLAGS_RELWITHDEBINFO=/DEBUG /INCREMENTAL:NO /LTCG /OPT:REF /OPT:ICF"
fi

echo "== Building avrdude in $bdir for $py${prefix:+ (deps: $prefix)}"
# shellcheck disable=SC2086
cmake -S "$src" -B "$bdir" "$@" $AVRDUDE_CMAKE_ARGS
cmake --build "$bdir" -j 8 --config RelWithDebInfo

b="$bdir/src"
if [ ! -f "$b/swig_avrdude.py" ]; then
    echo "SWIG module was not built. Is swig installed and were the Python" >&2
    echo "headers found? (see 'Python support' in the cmake output)" >&2
    exit 1
fi

mkdir -p "$here/lib"
rm -f "$here"/lib/_swig_avrdude*
if [ "$ostype" = windows ]; then
    cp "$b"/RelWithDebInfo/_swig_avrdude*.pyd "$here/lib/"    # multi-config generator
else
    cp "$b"/_swig_avrdude*.so "$here/lib/"
fi
cp "$b"/swig_avrdude.py "$b"/avrdude.conf "$here/lib/"
echo "== Copied libavrdude Python module and avrdude.conf to $here/lib"
ls -l "$here/lib"
