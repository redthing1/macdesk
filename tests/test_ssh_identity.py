"""Host transport regression: an instance identity survives forwarded port changes."""

import contextlib
import socket
import tempfile
import threading
import unittest
from pathlib import Path

import paramiko

from macdesk.guest import connect


class Authentication(paramiko.ServerInterface):
    def check_auth_password(self, username, password):
        if (username, password) == ("fixture", "fixture"):
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"


@contextlib.contextmanager
def server(key):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    stopped = threading.Event()
    transports = []

    def serve():
        stream, _ = listener.accept()
        transport = paramiko.Transport(stream)
        transports.append(transport)
        try:
            transport.add_server_key(key)
            transport.start_server(server=Authentication())
            while not stopped.wait(0.01) and transport.is_active():
                pass
        except (EOFError, paramiko.SSHException):
            pass
        finally:
            transport.close()

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        yield port
    finally:
        stopped.set()
        listener.close()
        for transport in transports:
            transport.close()
        worker.join(timeout=5)


class IdentityTests(unittest.TestCase):
    def test_port_rotation_preserves_key_verification_and_changed_key_is_rejected(self):
        key = paramiko.RSAKey.generate(2048)
        changed = paramiko.RSAKey.generate(2048)
        with tempfile.TemporaryDirectory() as directory:
            known = Path(directory) / "known_hosts"
            for _ in range(2):
                with server(key) as port:
                    client = connect(port, "fixture", known, password="fixture")
                    self.assertTrue(client.get_transport().is_authenticated())
                    client.close()
            self.assertEqual(list(paramiko.HostKeys(str(known))), ["macdesk"])
            with server(changed) as port, self.assertRaises(paramiko.BadHostKeyException):
                connect(port, "fixture", known, password="fixture")


if __name__ == "__main__":
    unittest.main()
