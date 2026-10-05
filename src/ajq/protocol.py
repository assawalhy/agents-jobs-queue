"""JSON-lines request/response protocol over a unix socket.

One request per connection, one response per connection. `wait` is the one
long-lived op: the daemon holds the connection open until the job is terminal.

Request:  {"op": "submit", ...}
Response: {"ok": true, ...} | {"ok": false, "error": "..."}
"""

from __future__ import annotations

import json
import socket
from typing import Any, Callable, Optional

Handler = Callable[[dict], dict]

# Every op the daemon understands.
OPS = (
    "ping",
    "submit",
    "status",
    "list",
    "cancel",
    "wait",
    "stats",
    "estimates_clear",
    "shutdown",
)


class ProtocolError(RuntimeError):
    pass


class DaemonUnavailable(ProtocolError):
    """No daemon is listening on the socket."""


def encode(payload: dict) -> bytes:
    """Single-line JSON bytes, newline terminated."""
    return (json.dumps(payload, separators=(",", ":"), default=str) + "\n").encode()


def request(sock_path: str, payload: dict, timeout: float = 30.0) -> dict:
    """Send one request and return the decoded response.

    Raises DaemonUnavailable when nothing is listening, ProtocolError on a
    malformed or truncated reply.
    """
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        try:
            client.connect(sock_path)
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise DaemonUnavailable(str(exc)) from exc
        client.sendall(encode(payload))
        buffer = b""
        while not buffer.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            buffer += chunk
    finally:
        client.close()
    text = buffer.decode("utf-8", "replace").strip()
    if not text:
        raise ProtocolError("empty response")
    try:
        response = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON response: {text[:200]}") from exc
    if not isinstance(response, dict):
        raise ProtocolError("response is not an object")
    return response


def read_request(conn: socket.socket) -> Optional[dict]:
    """Read one request line; None when the peer closed without sending."""
    buffer = b""
    while not buffer.endswith(b"\n"):
        chunk = conn.recv(65536)
        if not chunk:
            break
        buffer += chunk
    text = buffer.decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON request: {text[:200]}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("request is not an object")
    return payload


def write_response(conn: socket.socket, response: dict[str, Any]) -> None:
    conn.sendall(encode(response))


def serve(conn: socket.socket, handler: Handler) -> None:
    """Read one request, dispatch it, write one response."""
    try:
        payload = read_request(conn)
    except ProtocolError as exc:
        write_response(conn, {"ok": False, "error": str(exc)})
        return
    if payload is None:
        return
    try:
        response = handler(payload)
    except ProtocolError as exc:
        response = {"ok": False, "error": str(exc)}
    except Exception as exc:  # a bad request must not take the daemon down
        response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    write_response(conn, response if isinstance(response, dict) else {"ok": True})