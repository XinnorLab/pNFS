# SPDX-License-Identifier: MIT
"""Module registry (CON-02): the types a release ships, nothing loaded at runtime."""

from __future__ import annotations

from typing import Callable, Dict

from ..config import Instance
from .base import Module
from .fixture import FixtureModule
from .xinas import XinasModule

REGISTRY: Dict[str, Callable[[Instance], Module]] = {
    "xinas": XinasModule,
    "fixture": FixtureModule,
}


def create_module(instance: Instance) -> Module:
    factory = REGISTRY.get(instance.module)
    if factory is None:
        raise KeyError(instance.module)
    return factory(instance)
