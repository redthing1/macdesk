#!/usr/bin/env bash
set -euo pipefail
umask 077
prefix=${1:-${MACDESK_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/macdesk}/tools/virtiofsd}
if test -x "$prefix/bin/virtiofsd" && \
   test "$("$prefix/bin/virtiofsd" --version)" = 'virtiofsd 1.14.0'; then
    "$prefix/bin/virtiofsd" --version
    exit 0
fi
cargo install virtiofsd --version 1.14.0 --locked --root "$prefix"
"$prefix/bin/virtiofsd" --version
