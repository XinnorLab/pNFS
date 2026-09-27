# SPDX-License-Identifier: MIT
"""Canonical endpoint paths (2026-09-27 design §2)."""

import pytest

from lattice_ds_connector.paths import is_canonical


@pytest.mark.parametrize("path", ["/", "/a", "/a/", "/a/b", "/mnt/data/training-a/pnfs-ds", "/a/.b", "/a/b..", "/a/..."])
def test_canonical(path):
    assert is_canonical(path)


@pytest.mark.parametrize("path", ["", "a", "a/b", "//", "/a//b", "/a/./b", "/a/../b", "/..", "/.", "/a/..", "/a/.", "/a//"])
def test_not_canonical(path):
    assert not is_canonical(path)


def _endpoint_schema(schema):
    """The batch schema's endpoint object (the one that declares ds_path)."""
    stack = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "ds_path" in node.get("properties", {}):
                return node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    raise AssertionError("no endpoint schema")


@pytest.mark.parametrize("field", ["export_path", "ds_path"])
@pytest.mark.parametrize("path, ok", [
    ("/", True), ("/mnt/data", True), ("/mnt/data/", True), ("/mnt/data/.hidden", True),
    ("/mnt/data/../data2", False), ("/mnt/./data", False), ("/mnt//data", False), ("/mnt/data/..", False), ("//", False),
])
def test_batch_schema_accepts_only_canonical_paths(schemas, field, path, ok):
    jsonschema = pytest.importorskip("jsonschema")
    ep = {"server": "s", "export_path": "/mnt/data", "protocol": "NFS", "transport": "TCP", "port": 2049}
    ep[field] = path
    assert jsonschema.Draft7Validator(_endpoint_schema(schemas["batch"])).is_valid(ep) is ok
