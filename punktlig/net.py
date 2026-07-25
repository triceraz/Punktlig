"""Tiny HTTP helpers on top of urllib. The project intentionally has zero runtime dependencies."""

import json
import urllib.request

from .config import CLIENT_NAME


def get(url, headers=None, timeout=90):
    h = {"ET-Client-Name": CLIENT_NAME}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def post_json(url, payload, headers=None, timeout=60):
    h = {"ET-Client-Name": CLIENT_NAME, "Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())
