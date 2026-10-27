"""One runtime owns QEMU, its private control socket, and each granted share."""

import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

from . import state


def qemu_value(value):
    value = str(value)
    if "\n" in value or "\x00" in value:
        raise ValueError("invalid QEMU path")
    return value.replace(",", ",,")


def arguments(config, image, generation, run, port, shares):
    ram = config["memory_gib"]
    machine = f"vmapple,uuid={image['ecid']},accel=tcg"
    argv = [
        config["qemu"],
        "-machine",
        machine + (",memory-backend=ram" if shares else ""),
        "-cpu",
        "max",
        "-smp",
        str(config["cpus"]),
        "-m",
        f"{ram}G",
        "-display",
        "none",
        "-monitor",
        "none",
        "-serial",
        "none",
        "-qmp",
        f"unix:{run}/qmp.sock,server=on,wait=off",
        "-action",
        "panic=pause,shutdown=pause",
        "-no-reboot",
        "-bios",
        image["firmware"],
        "-drive",
        f"if=pflash,format=raw,file.filename={qemu_value(generation / 'aux.img')},node-name=aux",
        "-drive",
        "if=pflash,format=qcow2,file.filename="
        + qemu_value(generation / "root.qcow2")
        + ",node-name=root",
        "-device",
        "vmapple-virtio-blk-pci,variant=aux,drive=aux,share-rw=on",
        "-device",
        "vmapple-virtio-blk-pci,variant=root,drive=root,share-rw=on",
        "-netdev",
        f"user,id=net,ipv6=off,hostfwd=tcp:127.0.0.1:{port}-:22",
        "-device",
        "virtio-net-pci,netdev=net,romfile=,x-ascending-capabilities=on",
    ]
    if shares:
        argv += ["-object", f"memory-backend-memfd,id=ram,size={ram}G,share=on"]
    for index, share in enumerate(shares):
        argv += [
            "-chardev",
            f"socket,id=fs{index},path={run}/fs{index}.sock",
            "-device",
            f"vhost-user-fs-pci,chardev=fs{index},tag={share['name']},x-ascending-capabilities=on",
        ]
    return argv


def share_arguments(config, share, socket_path):
    # Every guest user writes as the granting host user; host ownership is
    # presented as the build account. The daemon's namespace remains rootless.
    argv = [
        config["virtiofsd"],
        "--shared-dir",
        share["host"],
        "--socket-path",
        str(socket_path),
        "--cache",
        "never",
        "--thread-pool-size",
        "4",
        "--uid-map",
        f":0:{os.getuid()}:1:",
        "--gid-map",
        f":0:{os.getgid()}:1:",
        "--translate-uid",
        "squash-guest:0:0:4294967295",
        "--translate-gid",
        "squash-guest:0:0:4294967295",
        "--translate-uid",
        f"host:0:{config['guest_uid']}:1",
        "--translate-gid",
        f"host:0:{config['guest_gid']}:1",
    ]
    if share.get("readonly"):
        argv += ["--readonly"]
    restricted = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
    if restricted.exists() and restricted.read_text().strip() == "1":
        if not shutil.which("aa-exec"):
            raise RuntimeError(
                "rootless shares need the virtiofsd AppArmor userns profile and aa-exec"
            )
        # Ubuntu ships this application profile for the system virtiofsd. Use
        # the same profile for a selected build without changing global policy.
        argv = ["aa-exec", "-p", "virtiofsd", "--", *argv]
    return argv


class Machine:
    def __init__(self, path, config):
        self.path, self.config = path, config
        self.children = []
        self.logs = []
        self.process = None
        self.lock = threading.Lock()
        self.closing = False
        self.pumps = []
        self.dropped = {}

    def spawn(self, argv, log):
        with self.lock:
            if self.closing:
                raise RuntimeError("instance startup was cancelled")
            self.logs.append(log)
            child = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self.children.append(child)

            def drain():
                retained = dropped = 0
                with child.stdout:
                    while data := child.stdout.read1(65536):
                        allowed = max(0, 8 * 1024**2 - retained)
                        log.write(data[:allowed])
                        log.flush()
                        retained += min(len(data), allowed)
                        dropped += max(0, len(data) - allowed)
                self.dropped[Path(log.name).name] = dropped

            pump = threading.Thread(target=drain, daemon=True)
            self.pumps.append(pump)
            pump.start()
            return child

    def start(self):
        if platform.system() != "Linux":
            raise RuntimeError("macdesk requires a Linux host")
        image = state.load(state.home() / "images" / (self.config["image"] + ".json"))
        generation = self.path / state.load(self.path / "active.json")["generation"]
        run = self.path / "run"
        run.mkdir(mode=0o700, exist_ok=True)
        for stale in run.glob("*.sock"):
            stale.unlink()
        shares = self.config["shares"]
        for index, share in enumerate(shares):
            argv = share_arguments(self.config, share, run / f"fs{index}.sock")
            log = (run / f"virtiofsd{index}.log").open("wb")
            child = self.spawn(argv, log)
            deadline = time.monotonic() + 10
            while not (run / f"fs{index}.sock").exists():
                if self.closing or child.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"virtiofsd failed; inspect {log.name}")
                time.sleep(0.05)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
        argv = arguments(self.config, image, generation, run, self.port, shares)
        log = (run / "qemu.log").open("wb")
        self.process = self.spawn(argv, log)
        return self.port

    def close(self):
        with self.lock:
            self.closing = True
        for child in reversed(self.children):
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait()
        for pump in self.pumps:
            pump.join(timeout=5)
        for log in self.logs:
            log.close()
        if self.dropped:
            state.save(self.path / "run/log-drops.json", self.dropped)


class QMP:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(5)
        self.sock.connect(str(path))
        self.reader = self.sock.makefile("rb")
        self.events = []
        self.counter = 0
        self.read()
        self.command("qmp_capabilities")

    def read(self):
        line = self.reader.readline(1024 * 1024 + 1)
        if not line or len(line) > 1024 * 1024:
            raise RuntimeError("invalid QMP response")
        return json.loads(line)

    def command(self, command, arguments=None):
        self.counter += 1
        body = {"execute": command, "id": self.counter}
        if arguments is not None:
            body["arguments"] = arguments
        self.sock.sendall(json.dumps(body).encode() + b"\n")
        while True:
            result = self.read()
            if "event" in result:
                self.events.append(result)
                self.events = self.events[-128:]
            elif result.get("id") == self.counter:
                if "error" in result:
                    raise RuntimeError(result["error"])
                return result.get("return")

    def close(self):
        self.reader.close()
        self.sock.close()
