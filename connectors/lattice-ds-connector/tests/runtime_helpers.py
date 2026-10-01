# SPDX-License-Identifier: MIT
"""Shared runtime test helpers: a fake monotonic clock, a scripted
xinas-shaped module, and small builders for instances, configs and runtimes."""

import copy
from typing import List, Optional

import source_builder as sb
from conftest import make_binding, make_profile
from lattice_ds_connector.config import Config, Instance, RuntimeConfig
from lattice_ds_connector.log import Logger
from lattice_ds_connector.modules.base import CollectionError, Module, SourceBatch
from lattice_ds_connector.modules.xinas_policy import evaluate_result
from lattice_ds_connector.runtime import Runtime


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class ScriptedModule(Module):
    """A xinas-shaped module whose collect returns scripted results."""

    name = "xinas"

    def __init__(self, clock: FakeClock, controller: str = sb.CONTROLLER):
        self.clock = clock
        self.controller = controller
        self.results: List = []
        self.generation = 100
        self.epoch = "publisher-epoch-1"
        self.request_ms = 100
        self.collect_calls = 0

    def describe(self):
        return {"module": "xinas"}

    def validate(self, instance, profile):
        return []

    def push(self, result=None, error: Optional[CollectionError] = None, same_generation: bool = False):
        self.results.append((result, error, same_generation))

    def collect(self, deadline_s: float) -> SourceBatch:
        self.collect_calls += 1
        result, error, same = self.results.pop(0) if self.results else (sb.base_result(), None, False)
        if error is not None:
            raise error
        if not same:
            self.generation += 1
        result = copy.deepcopy(result)
        result["source_generation"] = self.generation
        result["server_epoch"] = self.epoch
        return SourceBatch(result, self.clock(), self.request_ms, self.epoch, self.generation, result["snapshot_status"])

    def evaluate(self, batch, profile, bindings):
        return evaluate_result(batch.payload, profile, bindings, self.controller, batch.request_duration_ms)


def make_instance(iid: str = "xi-01", module: str = "xinas", bindings=None) -> Instance:
    return Instance(
        id=iid,
        module=module,
        bindings=tuple(bindings or (
            make_binding(0, "training-a", "/mnt/data/training-a", "training-a:7"),
            make_binding(1, "training-b", "/mnt/data/training-b", "training-b:7"),
            make_binding(2, "training-c", "/mnt/data2/training-c", "training-c:7"),
        )),
        profile_id="xinas-mvp",
        expected_controller_id=sb.CONTROLLER,
        source=None,
    )


def make_config(instances, runtime: Optional[RuntimeConfig] = None, profile=None) -> Config:
    p = profile or make_profile()
    return Config(config_version="1.0", test_mode=True, runtime=runtime or RuntimeConfig(collect_deadline_ms=200, collect_interval_ms=1000), profiles={p.id: p}, instances=tuple(instances), digest="sha256:" + "0" * 64)


def make_runtime(clock: FakeClock, modules: dict, instances=None, runtime_cfg=None, wall_clock=None, logger=None, profile=None) -> Runtime:
    """``wall_clock`` (() -> aware UTC datetime) dates the state file; None
    is the real clock."""
    instances = instances or [make_instance()]
    return Runtime(
        make_config(instances, runtime_cfg, profile=profile),
        logger or Logger(stream=open("/dev/null", "w")),
        clock=clock,
        module_factory=lambda inst: modules[inst.id],
        wall_clock=wall_clock,
    )


def records(rt: Runtime, iid: str = "xi-01"):
    inst = next(i for i in rt.batch()["instances"] if i["connector_instance_id"] == iid)
    return inst, {a["ds_id"]: a for a in inst["assessments"]}


def settle(rt: Runtime, clock: FakeClock, iid: str = "xi-01", cycles: int = 2, gap_s: float = 6.0):
    """Run enough cycles for the hold-down to complete."""
    ir = rt.instances[iid]
    for _ in range(cycles):
        ir.run_cycle()
        clock.advance(gap_s)
