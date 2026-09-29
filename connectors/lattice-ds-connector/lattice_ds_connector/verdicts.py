# SPDX-License-Identifier: MIT
"""The verdict store (smart verdict retention design §4.2): the last VALID
verdict per binding, kept across cycles and configuration reloads, dropped
on a rebind or when its hold runs out.

One store per :class:`~lattice_ds_connector.runtime.Runtime`, shared by
every instance worker; a binding's key belongs to one configured instance.
During a reload the replaced worker and its successor briefly overlap (the
new one starts before the old one's bounded stop returns); a stopped worker
stores nothing, so only the successor writes the key after that. The lock
makes each call atomic; it does not order two workers' puts.

The runtime persists the store in the state file (:mod:`.state`); the file
takes only the entries whose hold has not run out, whatever lingers here.
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
    #: a ``runtime.PublishedAssessment``. Its ``hold_ms`` is the verdict's
    #: hold, fixed when the verdict was stored.
    published: Any
    #: ``allowed: false`` — held for ``critical_hold_ms``, otherwise ``verdict_hold_ms``.
    critical: bool
    #: The observation the hold counts from (monotonic seconds).
    hold_origin_mono: float
    profile_id: str
    #: Restored from the state file after a connector restart.
    restored: bool = False

    def remaining_ms(self, now_mono: float) -> int:
        """Hold left at ``now_mono``; an origin in the future counts as age 0.

        The hold is the one the verdict was published with
        (``published.hold_ms``), not the current profile's: the store and the
        published ``remaining_ttl_ms`` give one answer, and a changed hold
        takes effect with the next fresh verdict."""
        held = int((now_mono - self.hold_origin_mono) * 1000)
        return max(0, self.published.hold_ms - max(0, held))


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
