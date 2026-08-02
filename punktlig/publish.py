"""Publish the site's data file to object storage instead of to git.

The export is regenerated every ten minutes. Committing it produced 124 of
the repository's 168 commits, which buried every real change and made the
history unreadable. The file is not source: it is state, and state belongs
in a store that keeps one current value rather than every value it ever had.

Credentials are read from the environment, and never from the repository.
`run-site.cmd` loads them from a file that lives outside version control, so
the service key exists only on the machine that collects.
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

# Anything longer and the map lags the archive; anything shorter and every
# viewer pays for a fetch the data cannot yet have changed.
CACHE_SECONDS = 30

ENV_URL = "PUNKTLIG_SUPABASE_URL"
ENV_KEY = "PUNKTLIG_SUPABASE_KEY"
ENV_BUCKET = "PUNKTLIG_SUPABASE_BUCKET"
DEFAULT_BUCKET = "punktlig"
OBJECT_NAME = "data.json"


class NotConfigured(RuntimeError):
    """No storage credentials present, so publishing is not attempted."""


def load_env(path):
    """Read KEY=value lines into the environment without overwriting it.

    A deliberately small parser: no quoting rules, no interpolation, and no
    dependency. Values are taken verbatim so a key containing '=' survives.
    """
    path = Path(path)
    if not path.exists():
        return {}
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        found[name] = value
        os.environ.setdefault(name, value)
    return found


def settings(env=None):
    env = env if env is not None else os.environ
    url = (env.get(ENV_URL) or "").rstrip("/")
    key = env.get(ENV_KEY) or ""
    if not url or not key:
        raise NotConfigured(
            f"set {ENV_URL} and {ENV_KEY} to publish; without them the export "
            "is written locally and nothing is uploaded"
        )
    return url, key, env.get(ENV_BUCKET) or DEFAULT_BUCKET


def public_url(url, bucket=DEFAULT_BUCKET, name=OBJECT_NAME):
    return f"{url.rstrip('/')}/storage/v1/object/public/{bucket}/{name}"


def upload(payload, env=None, opener=None):
    """Overwrite the stored object with this payload. Returns its public URL.

    Uses upsert rather than delete-then-create so readers never see a moment
    where the file is missing.
    """
    url, key, bucket = settings(env)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    target = f"{url}/storage/v1/object/{bucket}/{OBJECT_NAME}"
    request = urllib.request.Request(
        target,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Cache-Control": f"max-age={CACHE_SECONDS}",
            "x-upsert": "true",
        },
    )
    send = opener or urllib.request.urlopen
    with send(request, timeout=60) as resp:
        resp.read()
    return public_url(url, bucket)
