#!/bin/sh
# Build the libraries libavrdude links against (libusb, libusb-compat,
# hidapi, libftdi1) from source for the current architecture, with a low
# macOS deployment target, so the app also runs on older macOS versions.
# (Homebrew bottles only support the macOS version they were built on.)
#
# Usage: MACOSX_DEPLOYMENT_TARGET=11.0 ci/build-macos-deps.sh <prefix>
# Needs: curl, cmake, pkg-config, a C compiler.

set -e
prefix=${1:?usage: $0 <install prefix>}
: "${MACOSX_DEPLOYMENT_TARGET:?set MACOSX_DEPLOYMENT_TARGET}"
arch=$(uname -m)
mkdir -p "$prefix"
prefix=$(cd "$prefix" && pwd)

LIBUSB_VERSION=1.0.29
LIBUSB_COMPAT_VERSION=0.1.8
HIDAPI_VERSION=0.15.0
LIBFTDI_VERSION=1.5

export CFLAGS="-arch $arch -mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET -O2"
export LDFLAGS="-arch $arch -mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET"
export PKG_CONFIG_PATH="$prefix/lib/pkgconfig"
jobs=$(sysctl -n hw.ncpu)

work=$(mktemp -d)
fetch() {   # url -> unpacked into $work (bsdtar detects the compression)
    echo "== $1"
    curl -fsSL "$1" | tar -x -C "$work" -f -
}
# absolute install names, so PyInstaller can resolve them without rpaths;
# CMAKE_POLICY_VERSION_MINIMUM lets CMake 4 configure older projects
cmake_build() {   # srcdir, extra args...
    s=$1; shift
    cmake -S "$s" -B "$s/_build" \
        -D CMAKE_BUILD_TYPE=Release \
        -D "CMAKE_INSTALL_PREFIX=$prefix" -D "CMAKE_PREFIX_PATH=$prefix" \
        -D "CMAKE_INSTALL_NAME_DIR=$prefix/lib" \
        -D "CMAKE_OSX_ARCHITECTURES=$arch" \
        -D "CMAKE_OSX_DEPLOYMENT_TARGET=$MACOSX_DEPLOYMENT_TARGET" \
        -D CMAKE_POLICY_VERSION_MINIMUM=3.5 "$@"
    cmake --build "$s/_build" -j "$jobs"
    cmake --install "$s/_build"
}

fetch "https://github.com/libusb/libusb/releases/download/v$LIBUSB_VERSION/libusb-$LIBUSB_VERSION.tar.bz2"
(cd "$work/libusb-$LIBUSB_VERSION" && ./configure -q --prefix="$prefix" --disable-static \
    && make -s -j "$jobs" && make -s install)

fetch "https://github.com/libusb/libusb-compat-0.1/releases/download/v$LIBUSB_COMPAT_VERSION/libusb-compat-$LIBUSB_COMPAT_VERSION.tar.bz2"
(cd "$work/libusb-compat-$LIBUSB_COMPAT_VERSION" && ./configure -q --prefix="$prefix" --disable-static \
    && make -s -j "$jobs" && make -s install)

fetch "https://github.com/libusb/hidapi/archive/refs/tags/hidapi-$HIDAPI_VERSION.tar.gz"
cmake_build "$work/hidapi-hidapi-$HIDAPI_VERSION" -D HIDAPI_BUILD_HIDTEST=OFF

fetch "https://www.intra2net.com/en/developer/libftdi/download/libftdi1-$LIBFTDI_VERSION.tar.bz2"
cmake_build "$work/libftdi1-$LIBFTDI_VERSION" -D FTDI_EEPROM=OFF -D EXAMPLES=OFF -D FTDIPP=OFF \
    -D PYTHON_BINDINGS=OFF -D DOCUMENTATION=OFF -D LINK_PYTHON_LIBRARY=OFF -D STATICLIBS=OFF

rm -rf "$work"
echo "== Installed into $prefix:"
for f in "$prefix"/lib/*.dylib; do
    [ -L "$f" ] && continue
    printf '%s  minos %s\n' "$(basename "$f")" \
        "$(otool -l "$f" | awk '/LC_BUILD_VERSION/{b=1} b&&/minos/{print $2; exit} /LC_VERSION_MIN_MACOSX/{v=1} v&&/version/{print $2; exit}')"
done
