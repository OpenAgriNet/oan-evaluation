"""Forward read-only Beckn /search calls to a real Vistaar network instead of a mock.

User direction (2026-11-22): when the real service is reachable, use it; mock only calls
that are personal or have side effects. The rule's then.env_file names the environment whose
BAP_ENDPOINT and network IDs to use (dev's env file, or production's for scheme search);
they replace the eval's own context IDs.
"""
from __future__ import annotations

import copy
import json
import urllib.request
from pathlib import Path

_ENVS: dict[str, dict[str, str]] = {}


def _env(path: str) -> dict[str, str]:
    if path not in _ENVS:
        values = {}
        for line in Path(path).read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
        _ENVS[path] = values
    return _ENVS[path]


def forward(body: dict, env_file: str) -> dict:
    env = _env(env_file)
    request = copy.deepcopy(body)
    context = request.setdefault("context", {})
    for key in ("bap_id", "bap_uri", "bpp_id", "bpp_uri"):
        if env.get(key.upper()):
            context[key] = env[key.upper()]
    url = env["BAP_ENDPOINT"].rstrip("/") + "/search"
    req = urllib.request.Request(url, data=json.dumps(request).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.loads(response.read())
