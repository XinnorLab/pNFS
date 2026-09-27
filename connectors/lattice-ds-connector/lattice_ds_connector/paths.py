# SPDX-License-Identifier: MIT
"""Path helpers shared by the config and the modules (string paths, no I/O)."""

from __future__ import annotations


def normalize(path: str) -> str:
    """Drop trailing '/' ('/' stays '/')."""
    return path.rstrip("/") or "/"


def is_canonical(path: str) -> bool:
    """Absolute, and no empty, '.' or '..' component (one trailing '/' is allowed)."""
    if not path.startswith("/"):
        return False
    body = path[1:-1] if len(path) > 1 and path.endswith("/") else path[1:]
    if body == "":
        return path == "/"
    return all(c not in ("", ".", "..") for c in body.split("/"))


def path_contains(parent: str, path: str) -> bool:
    """True when ``path`` is ``parent`` or lies under it (component-wise)."""
    if not parent or not path:
        return False
    if path == parent:
        return True
    if parent == "/":
        return path.startswith("/")
    return path.startswith(parent.rstrip("/") + "/")
