"""Per-instance supervisor. Slow guest/file work never holds the status dispatcher."""

import json
import os
import secrets
import signal
import socket
import socketserver
import sys
import threading
import time
import traceback

import paramiko

from . import protocol as wire
from . import state
from .guest import Guest, connect
from .machine import QMP, Machine


def run_guest(guest, argv, cwd=None):
    try:
        with guest.open(
            {"op": "run", "argv": argv, "cwd": cwd, "env": {}, "timeout": 60}
        ) as channel:
            channel.settimeout(75)
            wire.send(channel, wire.EOF)
            retained = bytearray()
            while True:
                kind, data = wire.receive(channel)
                if kind in (wire.STDOUT, wire.STDERR):
                    retained.extend(data[: max(0, 65536 - len(retained))])
                elif kind == wire.ERROR:
                    raise RuntimeError(json.loads(data)["error"])
                elif kind == wire.EXIT:
                    code = json.loads(data)["code"]
                    if code:
                        raise RuntimeError(
                            f"guest setup exited {code}: {retained.decode(errors='replace')}"
                        )
                    return bytes(retained)
    except TimeoutError as error:
        raise TimeoutError(f"guest preparation timed out in {argv[0]}; outcome unknown") from error


def bridge(client, channel):
    def upstream():
        try:
            while data := client.recv(65536):
                channel.sendall(data)
        except (OSError, EOFError):
            pass
        finally:
            channel.close()

    sender = threading.Thread(target=upstream, daemon=True)
    sender.start()
    try:
        while data := channel.recv(65536):
            client.sendall(data)
    finally:
        channel.close()
        try:
            client.shutdown(socket.SHUT_RD)
        except OSError:
            pass
        sender.join(timeout=5)


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False


class Runtime:
    def __init__(self, path):
        self.path = path
        self.config = state.load(path / "config.json")
        self.active = state.load(path / "active.json")
        self.bootstrap_password = (
            self.config["initial_access"]["password"] if not self.active.get("enrolled") else None
        )
        self.machine = Machine(path, self.config)
        self.stage = "starting"
        self.error = None
        self.guest = None
        self.client = None
        self.started = time.monotonic()
        self.done = threading.Event()
        self.stopping = threading.Lock()

    def status(self):
        process = self.machine.process
        if process and process.poll() is not None and self.stage not in ("stopping", "failed"):
            self.stage, self.error = "exited", f"QEMU exited with status {process.returncode}"
        if self.stage == "ready" and not self.client.get_transport().is_active():
            self.stage, self.error = "disconnected", "guest connection lost"
        return {
            "stage": self.stage,
            "elapsed_s": round(time.monotonic() - self.started, 1),
            "error": self.error,
            "pid": process.pid if process else None,
        }

    def boot(self):
        try:
            state.save(
                self.path / "shutdown.json",
                {"clean": False, "generation": self.active["generation"], "at": time.time()},
            )
            port = self.machine.start()
            self.stage = "booting"
            deadline = time.monotonic() + 1200
            last_error = None
            while not self.done.is_set():
                if self.machine.process.poll() is not None:
                    raise RuntimeError("QEMU exited; inspect the instance run/qemu.log")
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"guest readiness deadline exceeded: {last_error}")
                try:
                    keys = list(
                        dict.fromkeys(
                            key
                            for key in (
                                self.config.get("key"),
                                self.config["initial_access"].get("key"),
                            )
                            if key
                        )
                    )
                    self.client = connect(
                        port,
                        self.config["user"],
                        self.path / "known_hosts",
                        key=keys or None,
                        password=self.bootstrap_password,
                    )
                    break
                except paramiko.BadHostKeyException as error:
                    raise RuntimeError(
                        "guest SSH host key changed; instance retained for inspection"
                    ) from error
                except paramiko.AuthenticationException as error:
                    raise RuntimeError(
                        "guest SSH authentication failed; verify the image access credentials"
                    ) from error
                except (OSError, EOFError, paramiko.SSHException) as error:
                    last_error = str(error)
                    if self.client:
                        self.client.close()
                        self.client = None
                    self.stage = "booting"
                    self.done.wait(5)
            if self.done.is_set():
                return
            self.stage = "preparing helper"
            guest = Guest(self.client)
            guest.bootstrap()
            info = guest.call({"op": "info"})
            if (info["uid"], info["gid"]) != (self.config["guest_uid"], self.config["guest_gid"]):
                raise RuntimeError("guest account IDs do not match the configured share owner")
            self.guest_home = info["home"]
            self.enroll(guest, port)
            self.stage = "mounting shares"
            for share in self.config["shares"]:
                target = self.guest_home + "/.local/share/macdesk/mounts/" + share["name"]
                run_guest(guest, ["/bin/mkdir", "-p", target])
                argv = ["/usr/bin/sudo", "-n", "/sbin/mount_virtiofs"]
                if share.get("readonly"):
                    argv += ["-r"]
                run_guest(guest, argv + [share["name"], target])
                # Access is required; a successful mount call alone is not readiness.
                run_guest(guest, ["/bin/test", "-r", target])
                run_guest(guest, ["/bin/test", "-x", target])
            self.guest = guest
            self.stage = "ready"
        except BaseException as error:
            self.stage, self.error = "failed", str(error)
            traceback.print_exc()

    def enroll(self, guest, port):
        if self.active.get("enrolled"):
            return
        key_path = self.path / "id_rsa"
        if not key_path.exists():
            key = paramiko.RSAKey.generate(3072)
            key.write_private_key_file(str(key_path))
            key_path.chmod(0o600)
        key = paramiko.RSAKey.from_private_key_file(str(key_path))
        public = key.get_name() + " " + key.get_base64()
        run_guest(guest, ["/bin/mkdir", "-p", self.guest_home + "/.ssh"])
        with self.client.open_sftp() as sftp:
            folder = self.guest_home + "/.ssh"
            sftp.chmod(folder, 0o700)
            target = folder + "/authorized_keys"
            try:
                with sftp.open(target, "rb") as existing:
                    contents = existing.read(1024 * 1024 + 1)
                if len(contents) > 1024 * 1024:
                    raise RuntimeError("authorized_keys exceeds bootstrap bound")
            except FileNotFoundError:
                contents = b""
            if public.encode() not in contents.splitlines():
                with sftp.open(target, "ab") as output:
                    output.write(b"\n" + public.encode() + b"\n")
            sftp.chmod(target, 0o600)
        checked = connect(port, self.config["user"], self.path / "known_hosts", key=key_path)
        checked.close()
        # Persist the verified key before changing the bootstrap credential.
        self.config["key"] = str(key_path)
        state.save(self.path / "config.json", self.config)
        if self.bootstrap_password:
            run_guest(
                guest,
                [
                    "/usr/bin/sudo",
                    "-n",
                    "/usr/bin/dscl",
                    ".",
                    "-passwd",
                    "/Users/" + self.config["user"],
                    self.bootstrap_password,
                    secrets.token_urlsafe(32),
                ],
            )
        self.active["enrolled"] = True
        state.save(self.path / "active.json", self.active)

    def require_guest(self):
        if self.stage != "ready":
            raise RuntimeError(f"instance is {self.stage}: {self.error or 'wait for readiness'}")
        if not self.client.get_transport().is_active():
            self.stage = "disconnected"
            raise RuntimeError("guest connection lost; command was not started")
        return self.guest

    def working_directory(self, host_directory, requested=None):
        if requested is not None:
            return self.guest_path(requested)
        from pathlib import Path

        directory = Path(host_directory)
        for share in sorted(
            self.config["shares"], key=lambda item: len(item["host"]), reverse=True
        ):
            try:
                relative = directory.relative_to(share["host"])
                return (
                    self.guest_home
                    + "/.local/share/macdesk/mounts/"
                    + share["name"]
                    + "/"
                    + str(relative)
                )
            except ValueError:
                continue
        raise ValueError(
            "current directory is not shared; add a share or pass --cwd with a guest path"
        )

    def guest_path(self, value):
        if value == "~" or value.startswith("~/"):
            return self.guest_home + value[1:]
        if not value.startswith("/"):
            raise ValueError("guest paths must be absolute or start with ~/")
        return value

    def stop(self, force=False):
        if not self.stopping.acquire(blocking=False):
            raise RuntimeError("instance is already stopping")
        try:
            if force:
                self.stage = "stopping"
                self.done.set()
                return {"clean": False}
            if not self.client or not self.client.get_transport().is_active():
                raise RuntimeError(
                    "guest control is unavailable; wait for boot or use stop --force"
                )
            guest = Guest(self.client)
            self.stage = "stopping"
            qmp = QMP(self.path / "run/qmp.sock")
            try:
                # Subscribe before asking the guest, so a fast shutdown cannot be missed.
                qmp.command(
                    "set-action", {"shutdown": "pause", "reboot": "reset", "panic": "pause"}
                )
                if self.guest is None:
                    # Shutdown remains available before helper preparation succeeds.
                    # This fixed command is evaluated only by the guest SSH server.
                    channel = self.client.get_transport().open_session(timeout=120)
                    channel.exec_command("/usr/bin/sudo -n /sbin/shutdown -h now")
                else:
                    channel = guest.open(
                        {
                            "op": "run",
                            "argv": ["/usr/bin/sudo", "-n", "/sbin/shutdown", "-h", "now"],
                            "env": {},
                        }
                    )
                    wire.send(channel, wire.EOF)
                deadline = time.monotonic() + 240
                while time.monotonic() < deadline:
                    qmp.command("query-status")
                    if any(
                        event.get("event") == "SHUTDOWN" and event.get("data", {}).get("guest")
                        for event in qmp.events
                    ):
                        qmp.command("quit")
                        self.machine.process.wait(timeout=10)
                        state.save(
                            self.path / "shutdown.json",
                            {
                                "clean": True,
                                "generation": self.active["generation"],
                                "at": time.time(),
                            },
                        )
                        self.done.set()
                        return {"clean": True}
                    time.sleep(0.5)
                self.stage = "shutdown uncertain"
                raise TimeoutError(
                    "guest shutdown was not observed; VM retained; inspect status or stop --force"
                )
            finally:
                qmp.close()
                channel.close()
        finally:
            self.stopping.release()

    def serve(self):
        runtime = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                delegated = False
                last_progress = 0

                def progress(size, total):
                    nonlocal last_progress
                    try:
                        pending = self.request.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
                    except BlockingIOError:
                        pending = None
                    if pending == b"":
                        raise InterruptedError("transfer client disconnected before commit")
                    if pending:
                        raise ValueError("unexpected transfer input")
                    now = time.monotonic()
                    if now - last_progress >= 1 or size == total:
                        wire.send(self.request, wire.STARTED, {"bytes": size, "total": total})
                        last_progress = now

                try:
                    kind, data = wire.receive(self.request)
                    if kind != wire.REQUEST or len(data) > 256 * 1024:
                        raise ValueError("invalid local request")
                    body = json.loads(data)
                    op = body["op"]
                    if op == "status":
                        result = runtime.status()
                    elif op == "stop":
                        result = runtime.stop(body.get("force", False))
                    elif op == "run":
                        guest = runtime.require_guest()
                        cwd = runtime.working_directory(body["host_cwd"], body.get("cwd"))
                        body = {
                            "op": "run",
                            "argv": body["argv"],
                            "cwd": cwd,
                            "env": body["env"],
                            "timeout": body.get("timeout", 0),
                        }
                        # After handing off a mutation, never retry it on another channel.
                        delegated = True
                        with guest.open(body) as channel:
                            bridge(self.request, channel)
                        return
                    elif op == "put":
                        delegated = True
                        result = runtime.require_guest().put(
                            body["source"],
                            runtime.guest_path(body["destination"]),
                            body["replace"],
                            progress,
                        )
                    elif op == "get":
                        delegated = True
                        result = runtime.require_guest().get(
                            runtime.guest_path(body["source"]),
                            body["destination"],
                            body["replace"],
                            progress,
                        )
                    else:
                        raise ValueError("unknown runtime operation")
                    wire.send(self.request, wire.EXIT, result)
                except Exception as error:
                    try:
                        wire.send(
                            self.request,
                            wire.ERROR,
                            {
                                "error": str(error),
                                "outcome": "unknown" if delegated else "not started",
                            },
                        )
                    except OSError:
                        pass

        with state.lease(self.path / "runtime.lock", blocking=False):
            endpoint = self.path / "runtime.sock"
            endpoint.unlink(missing_ok=True)
            with Server(str(endpoint), Handler) as server:
                os.chmod(endpoint, 0o600)
                signal.signal(signal.SIGTERM, lambda *_: self.done.set())
                signal.signal(signal.SIGINT, lambda *_: self.done.set())
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                boot = threading.Thread(target=self.boot, daemon=True)
                boot.start()
                try:
                    self.done.wait()
                finally:
                    server.shutdown()
                    # Join startup before tearing down the processes it owns.
                    if self.client:
                        self.client.close()
                    self.machine.close()
                    boot.join(timeout=70)
                    endpoint.unlink(missing_ok=True)


def main():
    if len(sys.argv) != 2:
        raise SystemExit("an instance name is required")
    os.umask(0o077)
    Runtime(state.instance(sys.argv[1])).serve()


if __name__ == "__main__":
    main()
