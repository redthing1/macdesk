# Architecture

One local CLI owns one runtime per instance. There is no fleet daemon. The
runtime owns QEMU, each rootless virtiofsd process, private QMP and the persistent
SSH connection. Owner-only Unix sockets carry CLI requests. SSH is forwarded on
host loopback; guest-operation clients receive no lifecycle credentials.

| Component | Responsibility |
| --- | --- |
| `cli.py` | Commands, foreground streams, signals and exit status |
| `state.py` | Immutable image registration, writable generations and image publication |
| `machine.py` | Emulator arguments, shared block nodes, directory daemons and bounded logs |
| `runtime.py` | Instance ownership, readiness, authenticated guest access and operation dispatch |
| `guest.py` | Helper installation, SSH channels and verified file streams |
| `protocol.py`, `agent.mm` | Framing and native macOS execution/file commit |

Readiness requires authenticated guest access, a responding helper and readable
configured shares. A running emulator or open TCP port is insufficient. The SSH
host key is pinned to a stable per-instance identity across forwarded-port
changes. Enrollment verifies a new instance key before replacing a bootstrap
password. Image access remains private so reset can enroll a fresh generation.

Guest argv and environment travel as JSON metadata in bounded binary frames.
The helper uses native process creation and explicit arguments. It forwards
binary stdout/stderr, uses a bounded stdin window, and processes signals while
output is backpressured. Cancellation and deadlines target the ordinary owned
process group; deliberately detached services require their own lifecycle.
Disconnects do not authorize rerunning commands.

File bytes use separate pipelined SFTP channels. Hashing and transfers run outside
status dispatch. Uploads stage beside their destination; the native helper checks
SHA-256, permissions and durability before atomic rename. Downloads verify into
a local staging file before publication. File requests have explicit paths and
never evaluate guest arguments on Linux.

Directory grants use shared-memory virtio-fs. A namespace maps the daemon's root
to the granting Linux user; ID translation presents that owner as the macOS build
account. The daemon enforces read-only grants. Coherent live shares use
`cache=never`, which prevents executable file mappings. Sources can remain live
while APFS holds build output. Copies into guest-local storage remain independent
of their host source; sharing never silently changes into synchronization.

Each image records a matching disk/AUX/firmware/ECID tuple and read-only backing
dependencies. Each instance owns a writable qcow2 root and an independent AUX
copy. Boot and virtio storage reference the same QEMU block node. Reset publishes
one new generation pointer only after both files exist, retaining the previous
generation. Sealing requires a verified clean shutdown, checks the new image and
compares its contents before publication. Bases are never cleanup targets.

The emulator builder pins its source and applies the repository patch. The
VMApple platform includes compatibility PAC handling and read-zero register
models; native build checks validate software behavior, not Apple hardware
security fidelity. The default profile is headless.
