"""Tiny HTTP helpers on top of urllib. The project intentionally has zero runtime dependencies."""

import json
import time
import urllib.request

from .config import CLIENT_NAME

# The socket timeout only bounds a single read. A server that trickles bytes
# forever passes that check on every read while the poll never finishes, so
# the whole body also gets a wall-clock deadline.
CHUNK = 1 << 16


def get(url, headers=None, timeout=30, deadline=120):
    h = {"ET-Client-Name": CLIENT_NAME}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        stop = time.monotonic() + deadline
        chunks = []
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            if time.monotonic() > stop:
                raise TimeoutError(
                    f"response body exceeded the {deadline}s deadline "
                    f"after {sum(len(c) for c in chunks)} bytes"
                )


def post_json(url, payload, headers=None, timeout=60):
    h = {"ET-Client-Name": CLIENT_NAME, "Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())
