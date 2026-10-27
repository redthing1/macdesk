#!/bin/sh
# Headless Apple Command Line Tools setup; do not install unrelated platforms.
set -eu
if /usr/bin/xcrun clang --version >/dev/null 2>&1 &&
   /usr/bin/xcrun --sdk macosx --show-sdk-path >/dev/null 2>&1; then
    exit 0
fi
selected=$(/usr/bin/xcode-select -p 2>/dev/null || true)
case "$selected" in
    */Xcode*.app/Contents/Developer)
        /usr/bin/sudo -n /usr/bin/xcodebuild -license accept
        if /usr/bin/xcrun clang --version >/dev/null 2>&1; then exit 0; fi
        ;;
esac
marker=/tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
created=0
if ! test -e "$marker"; then
    /usr/bin/sudo -n /usr/bin/touch "$marker"
    created=1
fi
trap 'if test "$created" = 1; then /usr/bin/sudo -n /bin/rm -f "$marker"; fi' EXIT HUP INT TERM
label=$(LC_ALL=C /usr/sbin/softwareupdate -l |
    /usr/bin/awk -F': ' '/Label: Command Line Tools/{print $2}' |
    /usr/bin/sort -V | /usr/bin/tail -n 1)
if test -z "$label"; then
    echo 'Apple Software Update did not offer Command Line Tools for this macOS version.' >&2
    exit 1
fi
/usr/bin/sudo -n /usr/sbin/softwareupdate -i "$label"
/usr/bin/sudo -n /usr/bin/xcode-select --switch /Library/Developer/CommandLineTools
/usr/bin/xcrun clang --version
/usr/bin/xcrun --sdk macosx --show-sdk-path
