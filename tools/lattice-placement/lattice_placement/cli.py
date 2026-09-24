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
    return p


def main(argv: Optional[List[str]] = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return EXIT_ERROR
    return args.func(args)
