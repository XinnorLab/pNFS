# SPDX-License-Identifier: MIT
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from lattice_ds_connector.config import Binding, Endpoint, Profile, sha256_digest  # noqa: E402

import source_builder as sb  # noqa: E402


def make_profile(**over) -> Profile:
    p = Profile(id="xinas-mvp", version="1", **over)
    return Profile(**{**p.__dict__, "digest": sha256_digest(p.placement_fields())})


def make_binding(ds_id: int, target: str, path: str, incarnation=None, networks=("10.10.10.0/24", "10.10.20.0/24"), security=("sys",), gen: int = 1) -> Binding:
    return Binding(
        ds_id=ds_id,
        binding_generation=gen,
        target_id=target,
        endpoint=Endpoint(server="10.10.10.21", export_path=path),
        expected_target_incarnation=incarnation,
        expected_client_networks=tuple(networks),
        expected_security=tuple(security),
    )


@pytest.fixture
def profile() -> Profile:
    return make_profile()


@pytest.fixture
def bindings():
    return (
        make_binding(0, "training-a", "/mnt/data/training-a", "training-a:7"),
        make_binding(1, "training-b", "/mnt/data/training-b", "training-b:7"),
        make_binding(2, "training-c", "/mnt/data2/training-c", "training-c:7"),
    )


@pytest.fixture
def base_result():
    return sb.base_result()


@pytest.fixture
def token_file(tmp_path):
    p = tmp_path / "xi-01.token"
    p.write_text("tok-viewer\n")
    p.chmod(0o600)
    return str(p)


@pytest.fixture
def schemas():
    with open(os.path.join(ROOT, "contracts", "connector-batch.schema.json"), encoding="utf-8") as fh:
        batch = json.load(fh)
    with open(os.path.join(ROOT, "contracts", "xinas-observations.schema.json"), encoding="utf-8") as fh:
        source = json.load(fh)
    return {"batch": batch, "source": source}


def validate(schema, doc):
    jsonschema = pytest.importorskip("jsonschema")
    v = jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker())
    errors = sorted(v.iter_errors(doc), key=lambda e: list(e.path))
    assert not errors, "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:5])
