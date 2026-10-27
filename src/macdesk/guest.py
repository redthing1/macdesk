"""Guest operations over an authenticated SSH transport, separate from VM lifecycle."""

import hashlib
import importlib.resources
import json
import os
import posixpath
import socket
import stat
import time
import uuid
from pathlib import Path

import paramiko

from . import protocol as wire

AGENT_COMMAND = 'exec "$HOME/.local/libexec/macdesk-agent"'


class EnrolledHostOnly(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        raise RuntimeError("guest host key is not enrolled; inspect the instance known_hosts file")


class Guest:
    def __init__(self, client):
        self.client = client

    def open(self, body):
        channel = self.client.get_transport().open_session(timeout=120)
        channel.exec_command(AGENT_COMMAND)
        wire.send(channel, wire.REQUEST, body)
        return channel

    def call(self, body):
        with self.open(body) as channel:
            while True:
                kind, data = wire.receive(channel)
                if kind == wire.ERROR:
                    raise wire.ProtocolError(json.loads(data)["error"])
                if kind == wire.EXIT:
                    return json.loads(data)
                if kind != wire.STARTED:
                    raise wire.ProtocolError("unexpected metadata response")

    def bootstrap(self):
        # All variable input is file content or JSON. This script runs only in macOS.
        channel = self.client.get_transport().open_session(timeout=120)
        channel.exec_command("/bin/sh -s")
        script = b"""set -eu
umask 077
mkdir -p "$HOME/.local/libexec" "$HOME/.local/share/macdesk/work" "$HOME/work"
"""
        channel.sendall(script)
        channel.shutdown_write()
        _drain(channel, 180)
        source = importlib.resources.files("macdesk").joinpath("agent.mm").read_bytes()
        digest = hashlib.sha256(source).hexdigest()
        with self.client.open_sftp() as sftp:
            home = sftp.normalize(".")
            source_path = home + "/.local/libexec/macdesk-agent.mm"
            receipt_path = home + "/.local/libexec/macdesk-agent.sha256"
            try:
                with sftp.open(receipt_path, "r") as receipt:
                    installed = receipt.read(128).decode().strip()
                if installed == digest and self.call({"op": "info"})["protocol"] == 1:
                    return
            except (OSError, EOFError, wire.ProtocolError):
                pass
            channel = self.client.get_transport().open_session(timeout=120)
            channel.exec_command("/bin/sh -s")
            channel.sendall(importlib.resources.files("macdesk").joinpath("clt.sh").read_bytes())
            channel.shutdown_write()
            _drain(channel, 1800)
            with sftp.open(source_path, "wb") as output:
                output.write(source)
            channel = self.client.get_transport().open_session(timeout=120)
            channel.exec_command("/bin/sh -s")
            channel.sendall(b"""set -eu
umask 077
cd "$HOME/.local/libexec"
/usr/bin/xcrun clang++ -std=c++20 -O2 -Wall -Wextra -Werror \\
  -Wno-deprecated-declarations -framework Foundation macdesk-agent.mm -o macdesk-agent.next
chmod 700 macdesk-agent.next
mv -f macdesk-agent.next macdesk-agent
""")
            channel.shutdown_write()
            _drain(channel, 600)
            if self.call({"op": "info"})["protocol"] != 1:
                raise RuntimeError("guest helper failed its readiness check")
            with sftp.open(receipt_path, "w") as receipt:
                receipt.write(digest + "\n")

    def put(self, source, destination, replace=False, progress=None):
        if not destination.startswith("/") or destination.endswith("/"):
            raise ValueError("upload destination must be an absolute guest file path")
        stage = posixpath.join(posixpath.dirname(destination), ".macdesk-" + uuid.uuid4().hex)
        started = time.monotonic()
        with Path(source).open("rb") as input_file, self.client.open_sftp() as sftp:
            initial = os.fstat(input_file.fileno())
            if not stat.S_ISREG(initial.st_mode):
                raise ValueError("upload source must be a regular file")
            sha = hashlib.sha256()
            size = 0
            sftp.get_channel().settimeout(30)
            try:
                if not replace:
                    try:
                        sftp.lstat(destination)
                    except FileNotFoundError:
                        pass
                    else:
                        raise FileExistsError(
                            "guest destination exists; use --replace to overwrite"
                        )
                with sftp.open(stage, "wx") as output:
                    output.set_pipelined(True)
                    while data := input_file.read(128 * 1024):
                        sha.update(data)
                        output.write(data)
                        size += len(data)
                        if progress:
                            progress(size, initial.st_size)
                final = os.fstat(input_file.fileno())
                if (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != (
                    final.st_size,
                    final.st_mtime_ns,
                    final.st_ctime_ns,
                ):
                    raise RuntimeError("upload source changed; destination was not replaced")
                info = self.call(
                    {
                        "op": "commit",
                        "stage": stage,
                        "path": destination,
                        "sha256": sha.hexdigest(),
                        "mode": initial.st_mode & 0o777,
                        "replace": replace,
                    }
                )
                return {**info, "elapsed_s": time.monotonic() - started}
            finally:
                try:
                    sftp.remove(stage)
                except OSError:
                    pass

    def get(self, source, destination, replace=False, progress=None):
        started = time.monotonic()
        info = self.call({"op": "stat", "path": source})
        destination = Path(destination).absolute()
        if not replace and os.path.lexists(destination):
            raise FileExistsError("host destination exists; use --replace to overwrite")
        stage = destination.with_name(".macdesk-" + uuid.uuid4().hex)
        sha = hashlib.sha256()
        size = 0
        try:
            with stage.open("xb") as output, self.client.open_sftp() as sftp:
                sftp.get_channel().settimeout(30)
                with sftp.open(source, "rb") as input_file:
                    input_file.prefetch(info["size"], max_concurrent_requests=32)
                    while data := input_file.read(128 * 1024):
                        sha.update(data)
                        size += len(data)
                        if size > info["size"]:
                            raise RuntimeError(
                                "download grew during transfer; destination preserved"
                            )
                        output.write(data)
                        if progress:
                            progress(size, info["size"])
                output.flush()
                os.fsync(output.fileno())
                os.fchmod(output.fileno(), info["mode"] & 0o777)
            if size != info["size"] or sha.hexdigest() != info["sha256"]:
                raise RuntimeError("download changed or failed verification; destination preserved")
            if replace:
                os.replace(stage, destination)
            else:
                os.link(stage, destination)
                stage.unlink()
            return {**info, "elapsed_s": time.monotonic() - started}
        finally:
            stage.unlink(missing_ok=True)


def _drain(channel, seconds):
    output = bytearray()
    deadline = time.monotonic() + seconds
    try:
        while True:
            for ready, recv in (
                (channel.recv_ready, channel.recv),
                (channel.recv_stderr_ready, channel.recv_stderr),
            ):
                if ready():
                    data = recv(65536)
                    if len(output) < 65536:
                        output.extend(data[: 65536 - len(output)])
            if (
                channel.exit_status_ready()
                and not channel.recv_ready()
                and not channel.recv_stderr_ready()
            ):
                code = channel.recv_exit_status()
                if code:
                    raise RuntimeError(
                        f"guest setup exited {code}: {output.decode(errors='replace')}"
                    )
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("guest setup did not finish; its outcome is unknown")
            time.sleep(0.02)
    finally:
        channel.close()


def connect(port, username, known_hosts, key=None, password=None):
    client = paramiko.SSHClient()
    if Path(known_hosts).exists():
        client.load_host_keys(str(known_hosts))
        client.set_missing_host_key_policy(EnrolledHostOnly())
    else:
        # First contact is to this owner's newly launched, loopback-only QEMU.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        transport_socket = socket.create_connection(("127.0.0.1", port), timeout=120)
        client.connect(
            "macdesk",
            sock=transport_socket,
            username=username,
            password=password,
            key_filename=(
                [str(item) for item in key] if isinstance(key, list) else str(key) if key else None
            ),
            allow_agent=False,
            look_for_keys=False,
            timeout=120,
            banner_timeout=120,
            auth_timeout=120,
        )
        client.save_host_keys(str(known_hosts))
        client.get_transport().set_keepalive(15)
        return client
    except BaseException:
        client.close()
        raise
