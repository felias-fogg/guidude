#!/bin/sh
# Fail if any Mach-O binary inside an app bundle requires a newer macOS than
# MACOSX_DEPLOYMENT_TARGET (e.g. a library accidentally taken from Homebrew).
#
# Usage: MACOSX_DEPLOYMENT_TARGET=11.0 ci/check-macos-minos.sh dist/GUIDude.app

set -e
app=${1:?usage: $0 <app bundle>}
target=${MACOSX_DEPLOYMENT_TARGET:?set MACOSX_DEPLOYMENT_TARGET}
arch=$(uname -m)
bad=0
n=0
for f in $(find "$app" -type f); do
    file -b "$f" | grep -q Mach-O || continue
    n=$((n + 1))
    # minos of the slice for this architecture (LC_BUILD_VERSION or the
    # older LC_VERSION_MIN_MACOSX)
    v=$(otool -arch "$arch" -l "$f" 2>/dev/null | awk '
        /LC_BUILD_VERSION/ {b=1} b && /minos/ {print $2; exit}
        /LC_VERSION_MIN_MACOSX/ {m=1} m && /^ *version/ {print $2; exit}')
    [ -z "$v" ] && continue
    newest=$(printf '%s\n%s\n' "$v" "$target" | sort -V | tail -1)
    if [ "$newest" != "$target" ]; then
        echo "::error::$f requires macOS $v (target $target)"
        bad=1
    fi
done
echo "Checked $n Mach-O files for $arch against macOS $target"
exit $bad
