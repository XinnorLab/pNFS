# SPDX-License-Identifier: MIT
"""`lattice-placement mode show | validate | set | verify` (design section 10).

Exit codes: 0 ready / same everywhere, 1 not ready / a difference, 2 an
argument, file or MDS could not be read.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from . import __version__, manifest as manifest_mod
from .ini import IniDocument
from .setmode import SetRefused, apply_plan, plan_set
from .validate import fold_preflight, run_preflight, validate_document

EXIT_OK = 0
EXIT_NOT_READY = 1
EXIT_ERROR = 2


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _parse_ds_list(text: Optional[str]) -> List[int]:
    out: List[int] = []
    for part in (text or "").split(","):
        part = part.strip()
        if part:
            out.append(int(part))
    return out


# ---------------------------------------------------------------------------
# mode validate
# ---------------------------------------------------------------------------

def cmd_validate(args: argparse.Namespace) -> int:
    m = manifest_mod.load()
    try:
        text = _read_text(args.config)
    except OSError as exc:
        print("ERROR: cannot read %s: %s" % (args.config, exc.strerror), file=sys.stderr)
        return EXIT_ERROR
    doc = IniDocument.parse(text)
    rep = validate_document(doc, args.mode, m, assume_set=not args.strict)
    if args.mode == "smart":
        pre = None
        if rep.ready or args.connector_socket:
            pre = run_preflight(args.connector_cli, args.connector_socket, _parse_ds_list(args.expect_ds))
        rep = fold_preflight(rep, pre)
    if args.json:
        print(json.dumps(rep.as_dict(), indent=2, sort_keys=True))
    else:
        print("READY" if rep.ready else "NOT_READY")
        for e in rep.errors:
            print("  error:   %s" % e)
        for w in rep.warnings:
            print("  warning: %s" % w)
        for k, v in rep.effective.items():
            print("  %s = %s" % (k, v))
        if rep.connector is not None:
            print("  connector: ready=%s reasons=%s" % (rep.connector.get("ready"),
                                                        ",".join(rep.connector.get("reasons", []) or [])))
    return EXIT_OK if rep.ready else EXIT_NOT_READY


# ---------------------------------------------------------------------------
# mode set
# ---------------------------------------------------------------------------

def _parse_kv(items: Optional[List[str]], what: str) -> dict:
    out = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError("%s expects key=value, got %r" % (what, item))
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def cmd_set(args: argparse.Namespace) -> int:
    m = manifest_mod.load()
    try:
        text = _read_text(args.config)
    except OSError as exc:
        print("ERROR: cannot read %s: %s" % (args.config, exc.strerror), file=sys.stderr)
        return EXIT_ERROR
    try:
        extra = _parse_kv(args.set, "--set")
        weights = {int(k): int(v) for k, v in _parse_kv(args.ds_weight, "--ds-weight").items()}
    except ValueError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return EXIT_ERROR
    try:
        plan = plan_set(text, args.mode, extra, m, legacy_policy=args.legacy_policy,
                        ds_weights=weights, path=args.config)
    except SetRefused as exc:
        if args.json:
            print(json.dumps({"applied": False, "refused": exc.errors}, indent=2))
        else:
            print("REFUSED")
            for e in exc.errors:
                print("  error:   %s" % e)
        return EXIT_NOT_READY
    result = None
    if args.apply and plan.changed:
        try:
            result = apply_plan(args.config, plan, m, args.audit_log)
        except SetRefused as exc:
            if args.json:
                print(json.dumps({"applied": False, "refused": exc.errors, "plan": plan.as_dict()}, indent=2))
            else:
                print("REFUSED")
                for e in exc.errors:
                    print("  error:   %s" % e)
            return EXIT_NOT_READY
        except OSError as exc:
            print("ERROR: %s" % exc, file=sys.stderr)
            return EXIT_ERROR
    if args.json:
        out = {"applied": result is not None, "plan": plan.as_dict()}
        if result is not None:
            out["backup"] = result.backup
            out["sha256_before"] = result.sha_before
            out["sha256_after"] = result.sha_after
            out["audit"] = result.audit_line
        print(json.dumps(out, indent=2))
    else:
        if not plan.changed:
            print("NO CHANGE: %s already describes mode %s" % (args.config, plan.mode))
        elif result is None:
            print("DRY RUN (add --apply to write %s): %s -> %s" % (args.config, plan.old_mode, plan.mode))
            print(plan.diff, end="" if plan.diff.endswith("\n") else "\n")
        else:
            print("applied: %s (backup %s)" % (result.path, result.backup))
            print("  sha256 before %s" % result.sha_before)
            print("  sha256 after  %s" % result.sha_after)
            print("  running pnfs-mds still uses mode %s; restart it inside the maintenance window "
                  "(docs/placement-modes/operations.md), then run `mode verify`" % plan.old_mode)
        for w in plan.warnings:
            print("  WARNING: %s" % w)
        for n in plan.notes:
            print("  NOTE: %s" % n)
    return EXIT_OK


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lattice-placement",
                                description="operator helper for pnfs-lattice placement modes")
    p.add_argument("--version", action="version", version="lattice-placement %s" % __version__)
    top = p.add_subparsers(dest="group")
    mode = top.add_parser("mode", help="placement mode commands")
    sub = mode.add_subparsers(dest="command")

    v = sub.add_parser("validate", help="read-only preflight of an mds.conf for a mode")
    v.add_argument("mode", choices=["rr", "fill", "smart", "legacy"])
    v.add_argument("--config", required=True, help="the mds.conf to check")
    v.add_argument("--strict", action="store_true",
                   help="judge the file as it is (default: as `mode set` would leave it)")
    v.add_argument("--connector-socket", default=None,
                   help="smart: the local connector socket for the preflight")
    v.add_argument("--connector-cli", default="lattice-ds-connector",
                   help="smart: the connector CLI (default: lattice-ds-connector on PATH)")
    v.add_argument("--expect-ds", default=None, help="smart: DS ids the connector must cover, e.g. 0,1")
    v.add_argument("--json", action="store_true")
    v.set_defaults(func=cmd_validate)

    s = sub.add_parser("set", help="rewrite ONE local mds.conf for a mode (dry-run unless --apply)")
    s.add_argument("mode", choices=["rr", "fill", "smart", "legacy"])
    s.add_argument("--config", required=True, help="the mds.conf to rewrite")
    s.add_argument("--apply", action="store_true", help="write the file (default: print the diff)")
    s.add_argument("--set", action="append", metavar="KEY=VALUE",
                   help="a managed key for rr/fill/smart (contract-manifest keys only); repeatable")
    s.add_argument("--legacy-policy", default="wrr", help="legacy: placement_policy value (default wrr)")
    s.add_argument("--ds-weight", action="append", metavar="ID=WEIGHT", help="legacy: ds_weight.<id>; repeatable")
    s.add_argument("--audit-log", default=manifest_mod.load().cli.get("audit_log", "/var/lib/lattice-placement/audit.log"))
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_set)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return EXIT_ERROR
    return args.func(args)
