#!/bin/sh
# Install only the native C/C++ build tools; the macOS SDK comes from Apple CLT.
set -eu
umask 022
test "$(uname -m)" = arm64
xcrun --find clang
xcrun --sdk macosx --show-sdk-path

cache="$HOME/Library/Caches/macdesk"
tools="$HOME/.local/share/macdesk/tools"
mkdir -p "$cache" "$tools" "$HOME/.local/bin" "$HOME/work"

fetch() {
    url=$1 file=$2 expected=$3
    if test -f "$cache/$file" && printf '%s  %s\n' "$expected" "$cache/$file" | shasum -a 256 -c -; then
        return
    fi
    for attempt in 1 2; do
        curl --fail --location --retry 3 --continue-at - "$url" -o "$cache/$file.partial"
        if printf '%s  %s\n' "$expected" "$cache/$file.partial" | shasum -a 256 -c -; then
            mv "$cache/$file.partial" "$cache/$file"
            return
        fi
        rm -f "$cache/$file.partial"
    done
    echo "download failed verification: $file" >&2
    exit 1
}

cmake_version=4.4.4
ninja_version=1.13.2
if ! test -x "$tools/cmake-$cmake_version/CMake.app/Contents/bin/cmake"; then
    file="cmake-$cmake_version-macos-universal.tar.gz"
    fetch "https://github.com/Kitware/CMake/releases/download/v$cmake_version/$file" "$file" \
        4b7b73704b1db9b374e5c9ab8e17ac6148b817b6396ee75cd94a852cbac9d305
    stage=$(mktemp -d "$tools/.cmake-XXXXXXXX")
    trap 'rm -rf "$stage"' EXIT HUP INT TERM
    tar -xzf "$cache/$file" --strip-components 1 -C "$stage"
    mv "$stage" "$tools/cmake-$cmake_version"
    trap - EXIT HUP INT TERM
fi
if ! test -x "$tools/ninja-$ninja_version/ninja"; then
    fetch "https://github.com/ninja-build/ninja/releases/download/v$ninja_version/ninja-mac.zip" \
        "ninja-$ninja_version.zip" c99048673aa765960a99cf10c6ddb9f1fad506099ff0a0e137ad8960a88f321b
    stage=$(mktemp -d "$tools/.ninja-XXXXXXXX")
    trap 'rm -rf "$stage"' EXIT HUP INT TERM
    unzip -q "$cache/ninja-$ninja_version.zip" -d "$stage"
    chmod 755 "$stage/ninja"
    mv "$stage" "$tools/ninja-$ninja_version"
    trap - EXIT HUP INT TERM
fi
for program in cmake ctest cpack; do
    ln -sfn "$tools/cmake-$cmake_version/CMake.app/Contents/bin/$program" "$HOME/.local/bin/$program"
done
ln -sfn "$tools/ninja-$ninja_version/ninja" "$HOME/.local/bin/ninja"
"$HOME/.local/bin/cmake" --version
"$HOME/.local/bin/ninja" --version

stage=$(mktemp -d "$HOME/.local/share/macdesk/.compiler-check-XXXXXXXX")
trap 'rm -rf "$stage"' EXIT HUP INT TERM
printf '#include <cstdio>\nint main() { std::puts("native SDK ready"); }\n' > "$stage/check.cpp"
xcrun clang++ -std=c++20 "$stage/check.cpp" -o "$stage/check"
"$stage/check"
