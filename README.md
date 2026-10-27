# macdesk

Headless ARM64 macOS build sandboxes on Linux. Keep a working Mac warm, run
native programs, share selected host directories, and copy files in either
direction. Images preserve prepared environments; independent writable instances
provide working state. x86-64 Linux uses QEMU/TCG CPU emulation.

Requires Python 3.11+, uv, and the patched VMApple emulator. The builders need
Git, a C/C++ compiler, Python venv support, Ninja, Rust, and development packages
for GLib, pixman, slirp, nettle, libcap-ng and libseccomp.

```sh
uv tool install .
tools/build-emulator.sh
tools/build-virtiofsd.sh
```

Import an installed macOS 27 ARM64 image with matching disk, AUX, firmware and
ECID. Inputs and backing files must be read-only and remain in place. AUX is the
32 MiB payload; the initial account needs SSH and noninteractive sudo access.
Image acquisition is currently a separate preparation step.

```sh
macdesk image import base --disk "$DISK" --format qcow2 \
  --aux "$AUX" --firmware "$FIRMWARE" --ecid "$ECID"
macdesk create --image base --user "$GUEST_USER" \
  --bootstrap-password-env BOOTSTRAP_PASSWORD
```

Set the named environment variable to the image's initial password before
creation, or use `--key` with an authorized private key. Startup enrolls an
instance key and replaces the bootstrap password. Credentials stay in private
owner state. Defaults are four guest CPUs and 16 GiB RAM. Cold boots take minutes
under CPU emulation; subsequent commands reuse the running instance.

The native setup installs Apple Command Line Tools when needed, CMake and Ninja.
Tool versions and download hashes are pinned. Customize with an ordinary shell
script: `macdesk setup ./setup.sh`. Setup scripts execute inside macOS.

```sh
macdesk share add project .
macdesk setup native
cd examples/hello
macdesk run -- cmake --fresh --preset dev
macdesk run -- cmake --build --preset dev
macdesk run -- ctest --preset dev
macdesk get '~/work/macdesk-hello/build/hello' ./hello-macos
```

`run` maps the current directory through a granted share. `--cwd` selects an
absolute guest directory or a quoted `~/…` path. Arguments remain literal; name
`/bin/sh -c` explicitly when shell expansion is wanted. Stdin, stdout, stderr,
exit status, Ctrl-C and `--timeout SECONDS` follow the foreground command.

Live shares keep host edits visible. Their coherent mode does not support
file mappings, so place executable build output on guest-local APFS. The example
preset does this. Grant or remove directories while stopped; `share list`
reports them. Add `--readonly` to enforce a read-only grant at the host daemon.
The verified build profile uses one directory grant. Multi-share cold boots
remain experimental and can stall in macOS mount preparation; preparation has
a bounded wait and retains the instance on failure.

Individual transfers need no mount. They stream data, verify SHA-256 and publish
the destination after verification. Existing destinations require `--replace`.
An interrupted data stream preserves the existing destination; a lost commit
response has an explicitly unknown outcome and is never replayed automatically.
For larger source trees, use a guest-local copy to avoid repeated shared-filesystem
metadata operations.

```sh
macdesk put ./sources.tar '~/work/sources.tar'
macdesk run --cwd '~/work' -- tar -xf sources.tar
macdesk get '~/work/result.tar' ./result.tar
```

`stop` preserves working files and waits for a verified guest shutdown. `open`
starts that state again; `run`, `put` and `get` also start it when needed.
`seal NAME` saves a cleanly stopped environment as an immutable image, retaining
its backing dependencies. `create --image NAME` can reuse its enrolled access.
Use `-i NAME` before a command to select another instance.

`reset` requires a stopped instance, replaces root and AUX together and retains
the previous generation for recovery. Guest-local copies return to the image's
state. Writes through live host grants survive reset.

State lives in `$XDG_DATA_HOME/macdesk` or `~/.local/share/macdesk`; override with
`MACDESK_HOME`. `status` reads runtime state without booting or contacting the
guest. Metadata is JSON; command output stays unwrapped and progress goes to
stderr. See [architecture](doc/architecture.md) and [contributor instructions](AGENTS.md).
