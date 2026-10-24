"""Sanitise proxy-bypass environment variables for httpx.

httpx parses every entry in ``NO_PROXY``/``no_proxy`` as a URL pattern at
client creation time.  Bracketed IPv6 entries such as ``[::1]`` -- which are
common in container and CI environments -- make that parsing fail with
``InvalidURL: Invalid port: ':1]'`` before any request is even attempted,
so *every* provider in this package breaks at once.

Bare IPv6 literals (``::1``) parse fine in httpx, so the fix is simply to
drop the brackets.  This only rewrites the in-process environment; it does
not change the user's shell, and non-IPv6 entries are left untouched.
"""

from __future__ import annotations

import os

_NO_PROXY_KEYS = ("NO_PROXY", "no_proxy")


def sanitize_no_proxy_value(value: str) -> str:
    """Return ``value`` with brackets stripped from IPv6 no_proxy entries."""

    entries = []
    for raw_entry in str(value).split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        if entry.startswith("["):
            closing = entry.find("]")
            if closing > 0:
                # Keep anything after the bracket (e.g. a port suffix).
                entry = entry[1:closing] + entry[closing + 1 :]
        entries.append(entry)
    return ",".join(entries)


def sanitize_no_proxy_env() -> bool:
    """Normalise ``NO_PROXY``/``no_proxy`` in the current process.

    Returns True when at least one variable was changed.
    """

    changed = False
    for key in _NO_PROXY_KEYS:
        original = os.environ.get(key)
        if not original:
            continue
        sanitized = sanitize_no_proxy_value(original)
        if sanitized != original:
            os.environ[key] = sanitized
            changed = True
    return changed
