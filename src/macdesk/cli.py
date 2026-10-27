"""Terminal interface to a persistent ARM64 macOS workspace."""

import argparse
import importlib.resources
import io
import json
import math
import os
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import protocol as wire
from . import state


def endpoint(path):
    stream = socket.socket(socket.AF_UNIX)
    try:
        stream.connect(str(path / "runtime.sock"))
    except BaseException:
        stream.close()
        raise
    return stream


def call(path, request):
    with endpoint(path) as stream:

        def progress(body):
            if "bytes" in body:
                print(f"macdesk: {body['bytes']}/{body['total']} bytes", file=sys.stderr)

        return wire.request(stream, request, progress)


def status(path):
    try:
        return call(path, {"op": "status"})
    except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError, EOFError):
        return {"stage": "stopped"}


def ensure_open(path):
    with state.lease(path / "client.lock"):
        current = status(path)
        if current["stage"] == "stopped":
            with (path / "runtime.log").open("ab", buffering=0) as log:
                subprocess.Popen(
                    [sys.executable, "-m", "macdesk.runtime", path.name],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            deadline = time.monotonic() + 15
            while status(path)["stage"] == "stopped":
                if time.monotonic() > deadline:
                    raise TimeoutError(f"runtime did not start; inspect {path / 'runtime.log'}")
                time.sleep(0.1)
    deadline = time.monotonic() + 1250
    previous = None
    while True:
        current = status(path)
        if current["stage"] == "ready":
            return
        if current["stage"] in (
            "failed",
            "stopped",
            "disconnected",
            "shutdown uncertain",
            "exited",
        ):
            raise RuntimeError(
                f"instance is {current['stage']}: {current.get('error') or 'inspect status'}"
            )
        display = f"{current['stage']} ({int(current.get('elapsed_s', 0))}s)"
        if display != previous:
            print("macdesk: " + display, file=sys.stderr)
            previous = display
        if time.monotonic() > deadline:
            raise TimeoutError("readiness wait expired; the instance may still be booting")
        time.sleep(5)


def execute(path, argv, cwd, environment, input_file=None, timeout=0):
    if not math.isfinite(timeout) or not 0 <= timeout <= 86400:
        raise ValueError("--timeout must be between 0 and 86400 seconds")
    ensure_open(path)
    if not argv:
        raise ValueError("provide a guest program after --")
    with endpoint(path) as stream:
        wire.send(
            stream,
            wire.REQUEST,
            {
                "op": "run",
                "argv": argv,
                "cwd": cwd,
                "host_cwd": str(Path.cwd().resolve()),
                "env": environment,
                "timeout": timeout,
            },
        )
        writing = threading.Lock()
        window = threading.Condition()
        available = [256 * 1024]
        finished = threading.Event()
        input_failed = []

        def send_input():
            try:
                while not finished.is_set():
                    if input_file is None:
                        fd = sys.stdin.fileno()
                        if not select.select([fd], [], [], 0.1)[0]:
                            continue
                        # Do not leave a daemon thread holding Python's buffered
                        # stdin lock when a short command exits at a live terminal.
                        data = os.read(fd, 65536)
                    else:
                        data = input_file.read1(65536)
                    if not data:
                        break
                    with window:
                        window.wait_for(lambda: available[0] >= len(data) or finished.is_set())
                        if finished.is_set():
                            return
                        available[0] -= len(data)
                    with writing:
                        wire.send(stream, wire.STDIN, data)
                with writing:
                    wire.send(stream, wire.EOF)
            except (OSError, ValueError) as error:
                input_failed.append(str(error))

        def interrupt(signum, _frame):
            with writing:
                wire.send(stream, wire.SIGNAL, bytes([signum]))

        old_int = signal.signal(signal.SIGINT, interrupt)
        old_term = signal.signal(signal.SIGTERM, interrupt)
        worker = threading.Thread(target=send_input, daemon=True)
        worker.start()
        started = False
        try:
            while True:
                kind, data = wire.receive(stream)
                if kind == wire.STDOUT:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                elif kind == wire.STDERR:
                    sys.stderr.buffer.write(data)
                    sys.stderr.buffer.flush()
                elif kind == wire.STARTED:
                    started = True
                elif kind == wire.WINDOW:
                    if len(data) != 4:
                        raise wire.ProtocolError("invalid stdin flow-control frame")
                    with window:
                        available[0] += struct.unpack("!I", data)[0]
                        if available[0] > 256 * 1024:
                            raise wire.ProtocolError("invalid stdin acknowledgement")
                        window.notify_all()
                elif kind == wire.ERROR:
                    result = json.loads(data)
                    raise RuntimeError(
                        f"{result['error']} (outcome: {result.get('outcome', 'unknown')})"
                    )
                elif kind == wire.EXIT:
                    if input_failed:
                        raise RuntimeError(f"stdin delivery failed: {input_failed[0]}")
                    result = json.loads(data)
                    if result.get("output_incomplete"):
                        print(
                            "macdesk: output ended while detached descendants retained a pipe",
                            file=sys.stderr,
                        )
                    return result["code"]
                else:
                    raise wire.ProtocolError("unexpected execution response")
        except (EOFError, OSError) as error:
            outcome = (
                "unknown; inspect guest work before retrying"
                if started
                else "unknown; start was not confirmed"
            )
            raise RuntimeError(f"guest stream lost ({outcome}): {error}") from error
        finally:
            finished.set()
            with window:
                window.notify_all()
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            worker.join(timeout=1)
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)


def create(args):
    root = state.home() / "instances"
    root.mkdir(mode=0o700, exist_ok=True)
    path = root / state.name(args.instance)
    image = state.load(state.home() / "images" / (state.name(args.image) + ".json"))
    if path.exists():
        raise ValueError("instance already exists")
    path.mkdir(mode=0o700)
    try:
        qemu = executable(args.qemu)
        qemu_img = executable(args.qemu_img)
        access = image.get("access") or {}
        user = args.user or access.get("user")
        if not user:
            raise ValueError("provide --user for this imported image")
        key = args.key or access.get("key")
        password = (
            os.environ.get(args.bootstrap_password_env) if args.bootstrap_password_env else None
        )
        if args.bootstrap_password_env and password is None:
            raise ValueError(f"environment variable {args.bootstrap_password_env} is not set")
        if not key and password is None:
            raise ValueError("provide --key or --bootstrap-password-env for initial guest access")
        config = {
            "image": args.image,
            "qemu": qemu,
            "qemu_img": qemu_img,
            "virtiofsd": executable(args.virtiofsd) if args.virtiofsd else None,
            "user": user,
            "guest_uid": args.guest_uid
            if args.guest_uid is not None
            else access.get("guest_uid", 501),
            "guest_gid": args.guest_gid
            if args.guest_gid is not None
            else access.get("guest_gid", 20),
            "key": str(Path(key).resolve(strict=True)) if key else None,
            "cpus": args.cpus,
            "memory_gib": args.memory_gib,
            "initial_access": {
                "password": password,
                "key": str(Path(key).resolve()) if key else None,
            },
            "shares": [],
        }
        state.new_generation(path, image, qemu_img)
        state.save(path / "config.json", config)
    except BaseException:
        shutil.rmtree(path)
        raise


def executable(program):
    found = None
    if "/" not in program:
        tool = "virtiofsd" if program == "virtiofsd" else "qemu"
        managed = state.home() / "tools" / tool / "bin" / program
        if managed.is_file() and os.access(managed, os.X_OK):
            found = str(managed)
    if not found:
        found = shutil.which(program)
    if not found:
        raise ValueError(f"executable not found: {program}")
    return str(Path(found).resolve())


def share(args, path):
    if args.share_command == "list":
        return state.load(path / "config.json")["shares"]
    with state.lease(path / "runtime.lock", blocking=False):
        config = state.load(path / "config.json")
        if args.share_command == "add":
            state.name(args.name)
            host = Path(args.directory).resolve(strict=True)
            if not host.is_dir():
                raise ValueError("share source must be a directory")
            if not config["virtiofsd"]:
                config["virtiofsd"] = executable("virtiofsd")
            help_text = subprocess.check_output([config["virtiofsd"], "--help"], timeout=10)
            if b"--translate-uid" not in help_text or b"--readonly" not in help_text:
                raise ValueError(
                    "shares require virtiofsd 1.14 or newer; build it with tools/build-virtiofsd.sh"
                )
            if len(args.name) > 32:
                raise ValueError("share names must not exceed 32 characters")
            if any(item["name"] == args.name for item in config["shares"]):
                raise ValueError("share name already exists")
            config["shares"].append(
                {"name": args.name, "host": str(host), "readonly": args.readonly}
            )
        else:
            if not any(item["name"] == args.name for item in config["shares"]):
                raise ValueError("share does not exist")
            config["shares"] = [item for item in config["shares"] if item["name"] != args.name]
        state.save(path / "config.json", config)


def parser():
    root = argparse.ArgumentParser(prog="macdesk", description=__doc__)
    root.add_argument(
        "-i", "--instance", default="default", help="working instance (default: default)"
    )
    commands = root.add_subparsers(dest="command", required=True)
    image = commands.add_parser("image", help="register immutable installed images")
    images = image.add_subparsers(dest="image_command", required=True)
    imp = images.add_parser("import", help="register an installed disk/AUX/firmware/identity tuple")
    imp.add_argument("name")
    imp.add_argument("--disk", required=True)
    imp.add_argument("--format", choices=("raw", "qcow2"), default="raw")
    imp.add_argument("--aux", required=True)
    imp.add_argument("--firmware", required=True)
    imp.add_argument("--ecid", required=True, type=int)
    imp.add_argument("--qemu-img", default="qemu-img")
    new = commands.add_parser("create", help="create an independent writable instance")
    new.add_argument("--image", required=True)
    new.add_argument("--qemu", default="qemu-system-aarch64")
    new.add_argument("--qemu-img", default="qemu-img")
    new.add_argument("--virtiofsd")
    new.add_argument("--user")
    new.add_argument("--guest-uid", type=int)
    new.add_argument("--guest-gid", type=int)
    new.add_argument("--key")
    new.add_argument("--bootstrap-password-env")
    new.add_argument("--cpus", type=int, choices=range(1, 9), default=4)
    new.add_argument("--memory-gib", type=int, choices=(4, 8, 16, 32), default=16)
    mounts = commands.add_parser(
        "share", help="grant named host directories while the instance is stopped"
    )
    mounts = mounts.add_subparsers(dest="share_command", required=True)
    add = mounts.add_parser("add")
    add.add_argument("name")
    add.add_argument("directory")
    add.add_argument("--readonly", action="store_true")
    mounts.add_parser("list")
    remove = mounts.add_parser("remove")
    remove.add_argument("name")
    for command, help_text in (
        ("open", "start the instance and wait for guest readiness"),
        ("status", "inspect current state without starting the guest"),
        ("reset", "return a stopped instance to its image, retaining the previous generation"),
    ):
        commands.add_parser(command, help=help_text)
    stop = commands.add_parser("stop", help="shut down the guest and preserve working state")
    stop.add_argument(
        "--force", action="store_true", help="terminate without a clean guest shutdown"
    )
    seal = commands.add_parser(
        "seal", help="save a cleanly stopped environment as a reusable image"
    )
    seal.add_argument("name")
    run = commands.add_parser(
        "run", help="execute literal argv in a shared or guest-local directory"
    )
    run.add_argument("--cwd", help="guest directory: /... or ~/... (default: current host share)")
    run.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="set a guest environment variable",
    )
    run.add_argument(
        "--timeout",
        type=float,
        default=0,
        metavar="SECONDS",
        help="guest command deadline; 0 disables the deadline",
    )
    run.add_argument("argv", nargs=argparse.REMAINDER)
    setup = commands.add_parser("setup", help="run a setup script in the guest")
    setup.add_argument("script", help="native, or a local shell script")
    for command in ("put", "get"):
        copy = commands.add_parser(command, help="copy one regular file with SHA-256 verification")
        copy.add_argument("source")
        copy.add_argument("destination")
        copy.add_argument(
            "--replace", action="store_true", help="atomically replace an existing destination"
        )
    return root


def dispatch(args):
    if args.command == "image":
        state.import_image(
            args.name,
            args.disk,
            args.format,
            args.aux,
            args.firmware,
            args.ecid,
            executable(args.qemu_img),
        )
        return
    if args.command == "create":
        return create(args)
    path = state.instance(args.instance)
    if args.command == "status":
        return status(path)
    if args.command == "open":
        ensure_open(path)
        return status(path)
    if args.command == "share":
        return share(args, path)
    if args.command == "seal":
        with state.lease(path / "runtime.lock", blocking=False):
            print("macdesk: sealing and verifying the stopped environment", file=sys.stderr)
            state.seal(path, args.name)
            return {"image": args.name}
    if args.command == "stop":
        if status(path)["stage"] == "stopped":
            return {"stage": "stopped"}
        result = call(path, {"op": "stop", "force": args.force})
        deadline = time.monotonic() + 40
        while (path / "runtime.sock").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("stop was accepted; runtime cleanup is still in progress")
            time.sleep(0.1)
        return result
    if args.command == "reset":
        with state.lease(path / "runtime.lock", blocking=False):
            config = state.load(path / "config.json")
            image = state.load(state.home() / "images" / (config["image"] + ".json"))
            generation = state.new_generation(path, image, config["qemu_img"])
            return {
                "generation": generation.name,
                "previous": "retained for recovery",
                "host_shares": "host files are unaffected by reset",
            }
    if args.command == "run":
        env = {}
        for item in args.env:
            key, separator, value = item.partition("=")
            if not separator or not key or "\x00" in item:
                raise ValueError("--env requires KEY=VALUE")
            env[key] = value
        argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        return execute(path, argv, args.cwd, env, timeout=args.timeout)
    if args.command == "setup":
        if args.script == "native":
            script = importlib.resources.files("macdesk").joinpath("native.sh").read_bytes()
            with io.BufferedReader(io.BytesIO(script)) as source:
                return execute(path, ["/bin/sh", "-s"], "/", {}, source)
        with Path(args.script).open("rb") as source:
            return execute(path, ["/bin/sh", "-s"], "/", {}, source)
    if args.command in ("put", "get"):
        ensure_open(path)
        source, destination = args.source, args.destination
        if args.command == "put":
            source = str(Path(source).resolve(strict=True))
        else:
            destination = str(Path(destination).absolute())
        return call(
            path,
            {
                "op": args.command,
                "source": source,
                "destination": destination,
                "replace": args.replace,
            },
        )


def main():
    try:
        result = dispatch(parser().parse_args())
        if isinstance(result, int):
            raise SystemExit(result)
        if result is not None:
            print(json.dumps(result, indent=2))
    except KeyboardInterrupt:
        print("macdesk: interrupted; use status to inspect the instance", file=sys.stderr)
        raise SystemExit(130) from None
    except (OSError, ValueError, RuntimeError, EOFError, subprocess.SubprocessError) as error:
        print(f"macdesk: {error}", file=sys.stderr)
        raise SystemExit(1) from None
