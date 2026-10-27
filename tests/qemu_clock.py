"""Exercise timer rate without macOS media: uv run python tests/qemu_clock.py."""

import argparse
import subprocess
import tempfile
import time
from pathlib import Path

from macdesk import state

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--qemu", type=Path, default=state.home() / "tools/qemu/bin/qemu-system-aarch64"
)
args = parser.parse_args()
with tempfile.TemporaryDirectory(prefix="macdesk-clock-") as directory:
    fixture = Path(directory)
    subprocess.run(
        [
            "clang",
            "--target=aarch64-none-elf",
            "-c",
            str(Path(__file__).with_name("clock.S")),
            "-o",
            str(fixture / "clock.o"),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "llvm-objcopy",
            "-O",
            "binary",
            "-j",
            ".text.boot",
            str(fixture / "clock.o"),
            str(fixture / "clock.bin"),
        ],
        check=True,
        capture_output=True,
    )
    for filename in ("aux", "root"):
        (fixture / filename).write_bytes(bytes(16384))
    log = fixture / "serial.log"
    argv = [
        str(args.qemu),
        "-machine",
        "vmapple,uuid=1,accel=tcg",
        "-cpu",
        "max",
        "-smp",
        "1",
        "-m",
        "1G",
        "-nodefaults",
        "-display",
        "none",
        "-monitor",
        "none",
        "-serial",
        f"file:{log}",
        "-no-reboot",
        "-bios",
        str(fixture / "clock.bin"),
        "-drive",
        f"file={fixture}/aux,if=pflash,format=raw",
        "-drive",
        f"file={fixture}/root,if=pflash,format=raw",
    ]
    child = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    start = None
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            content = log.read_bytes() if log.exists() else b""
            if b"CLOCK_BEGIN" in content and start is None:
                start = time.monotonic()
            if b"CLOCK_END" in content:
                elapsed = time.monotonic() - start
                if not 9.925 <= elapsed <= 10.25:
                    raise AssertionError(
                        f"24 MHz guest timer drift: {elapsed:.6f}s for ten seconds"
                    )
                print(f"24 MHz counter/timer PASS: {elapsed:.6f}s for ten guest seconds")
                break
            if child.poll() is not None:
                raise RuntimeError(f"clock probe exited early: {content!r}")
            time.sleep(0.002)
        else:
            raise TimeoutError("synthetic clock probe did not finish")
        if child.wait(timeout=5) != 0:
            raise RuntimeError("synthetic guest poweroff failed")
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stderr.close()
