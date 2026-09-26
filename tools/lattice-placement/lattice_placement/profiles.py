# SPDX-License-Identifier: MIT
"""The MDS profile pin map, ``ds_connector_expected_profiles`` and the
``placement_connector_profiles`` row: ``id=digest[,id=digest...]``
(per-profile digest design §3). Same rule as the MDS's
pm_parse_profile_pins and the connector preflight."""

from __future__ import annotations

import re
from typing import Dict

PROFILE_ID_PATTERN = r"^[A-Za-z0-9._-]{1,63}$"
MAX_PROFILES = 8
#: PM_DIGEST_MAX in the MDS is 128 bytes including the NUL terminator, so
#: 127 is the longest digest string the MDS will accept.
MAX_DIGEST_LEN = 127


def parse_pins(text: str) -> Dict[str, str]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty value")
    out: Dict[str, str] = {}
    for item in text.split(","):
        if "=" not in item:
            raise ValueError("item %r is not id=digest" % item.strip())
        pid, dig = (s.strip() for s in item.split("=", 1))
        if not re.fullmatch(PROFILE_ID_PATTERN, pid):
            raise ValueError("profile id %r must match [A-Za-z0-9._-]{1,63}" % pid)
        if not dig:
            raise ValueError("profile %s: empty digest" % pid)
        if len(dig) > MAX_DIGEST_LEN:
            raise ValueError("profile %s: digest must be 1..%d bytes" % (pid, MAX_DIGEST_LEN))
        if pid in out:
            raise ValueError("profile %s pinned twice" % pid)
        out[pid] = dig
    if len(out) > MAX_PROFILES:
        raise ValueError("more than %d profiles" % MAX_PROFILES)
    return dict(sorted(out.items()))


def format_pins(pins: Dict[str, str]) -> str:
    return ",".join("%s=%s" % kv for kv in sorted(pins.items())) or "-"
