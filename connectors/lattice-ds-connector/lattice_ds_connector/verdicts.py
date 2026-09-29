# SPDX-License-Identifier: MIT
"""The verdict store (smart verdict retention design §4.2): the last VALID
verdict per binding, kept across cycles and configuration reloads, dropped
on a rebind or when its hold runs out.

One store per :class:`~lattice_ds_connector.runtime.Runtime`, shared by
every instance worker; a binding's key belongs to exactly one instance, so
workers never contend for the same entry. The lock makes each call atomic.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class VerdictKey:
    """The binding a verdict belongs to; any change of it (a rebind, another
    target or pinned incarnation) is a different key (design rule 5)."""

    instance: str
    ds_id: int
    binding_generation: int
    target_id: str
    expected_target_incarnation: Optional[str]


@dataclass(frozen=True)
class StoredVerdict:
    #: The VALID record as published (after normalisation and hold-down):
    #: a ``runtime.PublishedAssessment``.
    published: Any
    #: ``allowed: false`` — held for ``critical_hold_ms``, otherwise ``verdict_hold_ms``.
    critical: bool
    #: The observation the hold counts from (monotonic seconds).
    hold_origin_mono: float
    profile_id: str
    #: Restored from the state file after a connector restart.
    restored: bool = False

    def remaining_ms(self, profile, now_mono: float) -> int:
        """Hold left at ``now_mono``; an origin in the future counts as age 0."""
        held = int((now_mono - self.hold_origin_mono) * 1000)
        return max(0, profile.hold_ms(self.critical) - max(0, held))


class VerdictStore:
    """Thread-safe map of :class:`VerdictKey` to :class:`StoredVerdict`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._v: Dict[VerdictKey, StoredVerdict] = {}
        self._version = 0

    def get(self, key: VerdictKey) -> Optional[StoredVerdict]:
        with self._lock:
            return self._v.get(key)

    def put(self, key: VerdictKey, v: StoredVerdict) -> None:
        with self._lock:
            self._v[key] = v
            self._version += 1

    def drop(self, key: VerdictKey, expected: Optional[StoredVerdict] = None) -> None:
        """Remove ``key``; with ``expected``, only while it still maps to that
        very entry (a caller that found it expired does not drop a newer one)."""
        with self._lock:
            current = self._v.get(key)
            if current is None or (expected is not None and current is not expected):
                return
            del self._v[key]
            self._version += 1

    def keys(self) -> List[VerdictKey]:
        with self._lock:
            return list(self._v)

    def items(self) -> List[Tuple[VerdictKey, StoredVerdict]]:
        with self._lock:
            return list(self._v.items())

    @property
    def version(self) -> int:
        """Bumps on every put and every drop that removed an entry."""
        with self._lock:
            return self._version
