#!/usr/bin/env bash
set -euo pipefail
umask 077

commit=509a4ce52bc703dfc79eb8b4283f51acd88b8594
repository=https://github.com/steelbrain/experiment-macOS-arm64-on-linux-x86.git
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
patch=$script_dir/qemu-macos27.patch
patch_sha=$(sha256sum "$patch" | cut -d' ' -f1)
cache=${MACDESK_CACHE_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}/macdesk}
prefix=${1:-${MACDESK_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/macdesk}/tools/qemu}
source=$cache/qemu-${commit:0:12}-${patch_sha:0:12}
mkdir -p "$source" "$prefix/bin"
exec 9>"$source/.build.lock"
flock 9
log=$source/build.log
echo "Preparing the pinned VMApple emulator; log: $log" >&2
(
    if ! test -d "$source/.git"; then
        git -C "$source" init
        git -C "$source" remote add origin "$repository"
        git -C "$source" fetch --depth 1 origin "$commit"
        git -C "$source" checkout --detach FETCH_HEAD
    fi
    test "$(git -C "$source" rev-parse HEAD)" = "$commit"
    if ! test -f "$source/.macdesk-patch"; then
        git -C "$source" apply --check "$patch"
        git -C "$source" apply "$patch"
        git -C "$source" diff --binary | sha256sum > "$source/.macdesk-patch"
    fi
    actual=$(git -C "$source" diff --binary | sha256sum)
    test "$actual" = "$(cat "$source/.macdesk-patch")" || {
        echo 'Cached emulator source was modified; preserve it and use a different cache directory.' >&2
        exit 1
    }
    mkdir -p "$source/build"
    cd "$source/build"
    ../configure --target-list=aarch64-softmmu --enable-tcg --enable-slirp \
        --enable-nettle --disable-gnutls --disable-docs --disable-werror \
        --disable-debug-info --disable-gtk --disable-sdl --disable-opengl \
        --disable-vnc --disable-spice --disable-guest-agent
    ninja -j "${JOBS:-8}" qemu-system-aarch64 qemu-img
    ./qemu-system-aarch64 -machine help | grep -q vmapple
    ldd ./qemu-system-aarch64 | grep -q libnettle
    for binary in qemu-system-aarch64 qemu-img; do
        install -m 755 "$binary" "$prefix/bin/.$binary.next"
        mv -f "$prefix/bin/.$binary.next" "$prefix/bin/$binary"
    done
    printf '%s\n%s\n' "$commit" "$patch_sha" > "$prefix/source.txt"
) > "$log" 2>&1 || {
    tail -n 30 "$log" >&2
    exit 1
}
echo "Emulator ready: $prefix/bin" >&2
