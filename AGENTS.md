# Working on macdesk

macdesk runs headless ARM64 macOS build sandboxes on Linux. One Python CLI owns
one runtime per instance. The runtime supervises QEMU and directory grants and
keeps an authenticated SSH transport. A native Objective-C++ helper executes
programs and commits uploaded files inside macOS. Read README.md and
doc/architecture.md before changing these boundaries.

Keep guest argv literal; never evaluate it in a host shell. Stream binary data
with bounded buffers. Status and cancellation must work during file transfers
and output backpressure. A lost response never authorizes replaying a mutation.
Preserve exit status and distinguish unknown outcomes from confirmed failure.

Images are immutable disk/AUX/firmware/identity tuples. Instances cold-boot
independent overlays. Reset replaces disk and AUX together and retains the old
generation. Keep lifecycle credentials and QMP private to the local owner.
Grant only explicitly selected host directories. Live host writes survive reset;
guest-local copies follow the guest disk. Keep build output on guest-local APFS
when using coherent live shares.

Run `uv run ruff format --check src tests`, `uv run ruff check src tests`,
`uv run python -m unittest discover -s tests -v`, and
`clang-format --dry-run --Werror src/macdesk/agent.mm`.
Runtime, helper and data changes also need the opt-in real macOS tests in
tests/test_macos_e2e.py. Use a disposable directory granted as writable `project`
and read-only `readonly`; set MACDESK_E2E=1 and MACDESK_E2E_SHARE to that directory.
Exercise the native build example after filesystem or emulator changes.
Emulator timer changes also require `uv run python tests/qemu_clock.py` against
the selected emulator; its firmware is synthetic and needs no macOS media.
Linux protocol tests do not prove macOS behavior. Record actual checks and
remaining limitations in ignored notes/.

Keep notes/ private and untracked. All other repository material is public:
concise, portable, and free of credentials, local machine details, private-note
links, or speculative claims.
