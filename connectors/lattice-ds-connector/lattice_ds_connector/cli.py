# SPDX-License-Identifier: MIT
"""Command line: ``run``, ``validate-config``, ``show``, ``describe``."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from typing import List, Optional

from . import __version__, contract
from .config import ConfigError, load_config
from .log import Logger
from .modules import REGISTRY
from .runtime import Runtime
from .server import Server, get_json

DEFAULT_CONFIG = "/etc/lattice-ds-connector/config.json"


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        for issue in exc.issues:
            print(f"error   {issue.path}: {issue.code} — {issue.message}")
        return 1
    for issue in config.warnings:
        print(f"warning {issue.path}: {issue.code} — {issue.message}")
    print(f"ok      config_digest={config.digest}")
    for pid, profile in config.profiles.items():
        print(f"ok      profile {pid} v{profile.version} digest={profile.digest}")
    total = sum(len(i.bindings) for i in config.instances)
    print(f"ok      {len(config.instances)} instance(s), {total} binding(s), test_mode={'true' if config.test_mode else 'false'}")
    return 0


def cmd_describe(_args: argparse.Namespace) -> int:
    out = {"connector": __version__, "contract_version": contract.CONTRACT_VERSION, "modules": sorted(REGISTRY)}
    print(json.dumps(out, indent=2))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    status, doc = get_json(args.socket, "/v1/assessments", timeout_s=2.0)
    if status != 200 or not isinstance(doc, dict):
        print(f"HTTP {status}: {json.dumps(doc)}")
        return 1
    if args.json:
        print(json.dumps(doc, indent=2))
        return 0
    print(f"runtime_epoch={doc['runtime_epoch']} config_digest={doc['config_digest']} generated_at={doc['generated_at']}")
    for inst in doc["instances"]:
        print(f"instance {inst['connector_instance_id']} ({inst['module_type']}) epoch={inst['epoch']} seq={inst['sequence']} snapshot={inst['snapshot_status']}")
        for a in inst["assessments"]:
            p = a["placement"]
            print(
                f"  ds {a['ds_id']:>3} gen {a['binding_generation']:<3} {a['target_id']:<24} "
                f"{a['quality']:<7} allowed={'yes' if p['allowed'] else 'no ':<3} ppm={p['multiplier_ppm']:<7} "
                f"age={a['evidence_age_ms']} ttl={a['remaining_ttl_ms']} reasons={','.join(p['reason_codes'])}"
            )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    logger = Logger()
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        for issue in exc.issues:
            logger.error("config_invalid", path=issue.path, code=issue.code, message=issue.message)
        return 2
    for issue in config.warnings:
        logger.warn("config_warning", path=issue.path, code=issue.code, message=issue.message)
    runtime = Runtime(config, logger)
    socket_path = args.socket or config.runtime.socket_path
    server = Server(runtime, socket_path, config.runtime.socket_group)
    stop = threading.Event()

    def on_stop(signum, _frame) -> None:
        logger.info("signal", signal=signum)
        stop.set()

    def on_reload(_signum, _frame) -> None:
        try:
            new_config = load_config(args.config)
        except ConfigError as exc:
            for issue in exc.issues:
                logger.error("reload_rejected", path=issue.path, code=issue.code, message=issue.message)
            logger.error("reload_rejected_summary", active_config_digest=runtime.config.digest)
            return
        runtime.reload(new_config)

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGHUP, on_reload)
    runtime.start()
    server.start()
    logger.info("serving", socket=socket_path)
    try:
        while not stop.is_set():
            stop.wait(1.0)
    finally:
        server.stop()
        runtime.stop(5.0)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lattice-ds-connector", description="Placement-assessment connector for pnfs-lattice")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command")
    run = sub.add_parser("run", help="run the daemon")
    run.add_argument("--config", default=DEFAULT_CONFIG)
    run.add_argument("--socket", default=None, help="override runtime.socket_path")
    run.set_defaults(func=cmd_run)
    val = sub.add_parser("validate-config", help="validate a configuration file and print its digests")
    val.add_argument("--config", default=DEFAULT_CONFIG)
    val.set_defaults(func=cmd_validate)
    show = sub.add_parser("show", help="print the current local batch")
    show.add_argument("--socket", default="/run/lattice-ds-connector/connector.sock")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_show)
    desc = sub.add_parser("describe", help="list the registered module types")
    desc.set_defaults(func=cmd_describe)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
