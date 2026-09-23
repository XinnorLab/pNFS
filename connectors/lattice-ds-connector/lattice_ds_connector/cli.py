# SPDX-License-Identifier: MIT
"""Command line: ``run``, ``validate-config``, ``show``, ``describe``, ``discover``."""

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
from .modules import REGISTRY, create_module
from .runtime import Runtime
from .server import Server, get_json

DEFAULT_CONFIG = "/etc/lattice-ds-connector/config.json"
_UNPINNED = "<unpinned>"  # placeholder discover uses to load a not-yet-pinned binding


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


def cmd_discover(args: argparse.Namespace) -> int:
    """Fetch each xinas instance's source once and print what a binding must
    pin: share ids, their current incarnation, export path and status. The
    operator copies the incarnation into `expected_target_incarnation`
    (audit C-05: bindings are pinned, never trusted on first use)."""
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        for issue in exc.issues:
            if issue.code == "INCARNATION_REQUIRED":
                continue  # discover exists to fill exactly that field
            print(f"error   {issue.path}: {issue.code} — {issue.message}")
        blocking = [i for i in exc.issues if i.code != "INCARNATION_REQUIRED"]
        if blocking:
            return 1
        from .config import validate_config_dict
        import json as _json

        with open(args.config, "r", encoding="utf-8") as fh:
            raw = _json.load(fh)
        for inst in raw.get("instances", []):
            for b in inst.get("bindings", []):
                b.setdefault("expected_target_incarnation", _UNPINNED)
        config, _issues = validate_config_dict(raw)
        if config is None:
            return 1
    rc = 0
    for inst in config.instances:
        if inst.module != "xinas" or inst.source is None:
            continue
        if args.instance and inst.id != args.instance:
            continue
        module = create_module(inst)
        try:
            batch = module.collect(config.runtime.collect_deadline_ms / 1000.0)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            print(f"instance {inst.id}: collect failed: {exc}")
            rc = 1
            continue
        result = batch.payload
        print(f"instance {inst.id}: controller {result.get('controller_id')} epoch {result.get('server_epoch')} gen {result.get('source_generation')} snapshot {result.get('snapshot_status')}")
        bound = {b.target_id: b for b in inst.bindings}
        for share in result.get("shares", []):
            b = bound.get(share.get("share_id"))
            pin = ""
            if b is not None:
                pinned = b.expected_target_incarnation
                pin = " (bound ds %d, pinned %s)" % (b.ds_id, "-" if pinned in (None, _UNPINNED) else pinned)
            print(
                f"  share {share.get('share_id')!s:<24} incarnation {share.get('incarnation')!s:<40} "
                f"path {share.get('export_path')} status {share.get('collection_status')} {share.get('reason_codes')}{pin}"
            )
    return rc


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
    disc = sub.add_parser("discover", help="fetch each xinas source once and print the share incarnations to pin")
    disc.add_argument("--config", default=DEFAULT_CONFIG)
    disc.add_argument("--instance", default=None)
    disc.set_defaults(func=cmd_discover)
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
