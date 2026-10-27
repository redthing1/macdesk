import json
import socket
import struct
import threading
import unittest

from macdesk import protocol as wire


class ProtocolTests(unittest.TestCase):
    def test_fragmented_binary_and_unicode_frames(self):
        left, right = socket.socketpair()
        payload = b"\x00\xff" + "spaces; $(literal) 日本語".encode()
        encoded = wire.HEADER.pack(wire.STDOUT, len(payload)) + payload

        def writer():
            with left:
                for byte in encoded:
                    left.sendall(bytes([byte]))

        worker = threading.Thread(target=writer)
        worker.start()
        with right:
            self.assertEqual(wire.receive(right), (wire.STDOUT, payload))
            with self.assertRaises(EOFError):
                wire.receive(right)
        worker.join()

    def test_truncated_body_and_oversized_header_fail_explicitly(self):
        for encoded, error in (
            (wire.HEADER.pack(wire.STDOUT, 5) + b"abc", EOFError),
            (wire.HEADER.pack(wire.STDOUT, wire.MAX_FRAME + 1), wire.ProtocolError),
            (struct.pack("!BI", 255, 0), wire.ProtocolError),
        ):
            with self.subTest(encoded=encoded):
                left, right = socket.socketpair()
                with left, right:
                    left.sendall(encoded)
                    left.shutdown(socket.SHUT_WR)
                    with self.assertRaises(error):
                        wire.receive(right)

    def test_lost_mutation_response_is_never_replayed(self):
        left, right = socket.socketpair()
        observed = []

        def server():
            with left:
                kind, data = wire.receive(left)
                observed.append((kind, json.loads(data)))
                # Mutation accepted, response lost.

        worker = threading.Thread(target=server)
        worker.start()
        with right, self.assertRaisesRegex(wire.ProtocolError, "outcome is unknown"):
            wire.request(right, {"op": "commit", "path": "/work/result"})
        worker.join()
        self.assertEqual(len(observed), 1)


if __name__ == "__main__":
    unittest.main()
