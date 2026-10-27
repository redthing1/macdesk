"""Bounded, binary-safe guest streams. No command is replayed after disconnect."""

import json
import struct

REQUEST, STDOUT, STDERR, EXIT, STDIN, EOF, SIGNAL, ERROR, STARTED, WINDOW = range(10)
MAX_FRAME = 1024 * 1024
HEADER = struct.Struct("!BI")


class ProtocolError(RuntimeError):
    pass


def read_exact(stream, size):
    chunks = bytearray()
    while len(chunks) < size:
        part = stream.recv(size - len(chunks))
        if not part:
            raise EOFError("connection closed before a complete response")
        chunks.extend(part)
    return bytes(chunks)


def receive(stream):
    kind, size = HEADER.unpack(read_exact(stream, HEADER.size))
    if kind > WINDOW or size > MAX_FRAME:
        raise ProtocolError("invalid or oversized frame")
    return kind, read_exact(stream, size)


def send(stream, kind, data=b""):
    if isinstance(data, dict):
        data = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    if kind > WINDOW or len(data) > MAX_FRAME:
        raise ProtocolError("invalid or oversized outgoing frame")
    stream.sendall(HEADER.pack(kind, len(data)) + data)


def request(stream, body, progress=None):
    send(stream, REQUEST, body)
    while True:
        try:
            kind, data = receive(stream)
        except EOFError as error:
            if body.get("op") in ("put", "get", "stop", "commit"):
                raise ProtocolError(
                    "response lost; operation outcome is unknown; inspect before retrying"
                ) from error
            raise
        if kind == ERROR:
            result = json.loads(data)
            suffix = f" (outcome: {result['outcome']})" if "outcome" in result else ""
            raise ProtocolError(result["error"] + suffix)
        if kind == EXIT:
            return json.loads(data)
        if kind != STARTED:
            raise ProtocolError("unexpected response to metadata request")
        if progress:
            progress(json.loads(data))
