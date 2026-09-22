# SPDX-License-Identifier: MIT
"""JSON-lines logging with per-key rate limiting and counters (CON-23).

Every line is one JSON object on stderr (journald keeps it). Repeated
failures of the same (instance, code) are logged once per ``interval`` with
a ``suppressed`` count, so a flapping source cannot flood the journal while
the counters still tell the whole story. Secrets never reach a log call:
callers pass codes and paths, not tokens or URLs with credentials.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, TextIO


class Counters:
    """Bounded-label counters exported by /metrics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: Dict[str, Dict[tuple, int]] = {}

    def inc(self, name: str, labels: Optional[Dict[str, str]] = None, by: int = 1) -> None:
        key = tuple(sorted((labels or {}).items()))
        with self._lock:
            series = self._values.setdefault(name, {})
            series[key] = series.get(key, 0) + by

    def snapshot(self) -> Dict[str, Dict[tuple, int]]:
        with self._lock:
            return {name: dict(series) for name, series in self._values.items()}


class Logger:
    def __init__(self, stream: Optional[TextIO] = None, interval_s: float = 10.0, clock=time.monotonic):
        self._stream = stream or sys.stderr
        self._interval = interval_s
        self._clock = clock
        self._lock = threading.Lock()
        self._last: Dict[str, float] = {}
        self._suppressed: Dict[str, int] = {}
        self.counters = Counters()

    def _emit(self, level: str, event: str, fields: Dict[str, Any]) -> None:
        line = {
            "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": level,
            "event": event,
            **fields,
        }
        try:
            self._stream.write(json.dumps(line, sort_keys=True, default=str) + "\n")
            self._stream.flush()
        except (OSError, ValueError):  # pragma: no cover - stderr gone
            pass

    def info(self, event: str, **fields: Any) -> None:
        self._emit("info", event, fields)

    def warn(self, event: str, **fields: Any) -> None:
        self._emit("warn", event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit("error", event, fields)

    def limited(self, level: str, event: str, key: str, **fields: Any) -> None:
        """Log at most once per interval per key; count what was suppressed."""
        now = self._clock()
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self._interval:
                self._suppressed[key] = self._suppressed.get(key, 0) + 1
                return
            suppressed = self._suppressed.pop(key, 0)
            self._last[key] = now
        if suppressed:
            fields = {**fields, "suppressed": suppressed}
        self._emit(level, event, fields)
