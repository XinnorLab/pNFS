# SPDX-License-Identifier: MIT
"""Line-preserving model of an `mds.conf` with exactly the grammar of the
MDS (`src/common/config.c`): whitespace stripped, `#` and `;` start a
comment, a line starting with `[` is skipped, the first `=` splits key and
value, a line without `=` is ignored, the last occurrence of a key wins.

Every line the helper does not own is rendered back byte for byte; the
keys it owns live in one managed block (design section 10)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

KIND_PAIR = "pair"
KIND_COMMENT = "comment"
KIND_BLANK = "blank"
KIND_SECTION = "section"
KIND_JUNK = "junk"


@dataclass
class Line:
    kind: str
    raw: str                      # original text without the newline
    key: Optional[str] = None
    value: Optional[str] = None


def _classify(raw: str) -> Line:
    s = raw.strip()
    if s == "":
        return Line(KIND_BLANK, raw)
    if s[0] in "#;":
        return Line(KIND_COMMENT, raw)
    if s[0] == "[":
        return Line(KIND_SECTION, raw)
    eq = s.find("=")
    if eq < 0:
        return Line(KIND_JUNK, raw)
    return Line(KIND_PAIR, raw, key=s[:eq].strip(), value=s[eq + 1:].strip())


class IniDocument:
    def __init__(self, lines: List[Line], trailing_newline: bool = True):
        self.lines = lines
        self.trailing_newline = trailing_newline

    @classmethod
    def parse(cls, text: str) -> "IniDocument":
        if text == "":
            return cls([], True)
        parts = text.split("\n")
        trailing = text.endswith("\n")
        if trailing:
            parts = parts[:-1]
        return cls([_classify(p) for p in parts], trailing)

    def render(self) -> str:
        out = "\n".join(l.raw for l in self.lines)
        if self.lines and self.trailing_newline:
            out += "\n"
        return out

    # -- reads -------------------------------------------------------------

    def items(self) -> List[Tuple[str, str]]:
        return [(l.key, l.value) for l in self.lines if l.kind == KIND_PAIR]  # type: ignore[misc]

    def get(self, key: str) -> Optional[str]:
        val = None
        for k, v in self.items():
            if k == key:
                val = v
        return val

    def count(self, key: str) -> int:
        return sum(1 for k, _ in self.items() if k == key)

    def prefixed(self, prefix: str) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for k, v in self.items():
            if k.startswith(prefix) and len(k) > len(prefix):
                out[k[len(prefix):]] = v
        return out

    def effective(self) -> Dict[str, str]:
        """Every key with its winning value, in first-seen order."""
        out: Dict[str, str] = {}
        for k, v in self.items():
            out[k] = v
        return out

    # -- edits -------------------------------------------------------------

    def remove_keys(self, keys: Iterable[str] = (), prefixes: Iterable[str] = ()) -> List[str]:
        keys = set(keys)
        prefixes = tuple(prefixes)
        removed: List[str] = []
        kept: List[Line] = []
        for l in self.lines:
            if l.kind == KIND_PAIR and l.key is not None and (
                l.key in keys or any(l.key.startswith(p) and len(l.key) > len(p) for p in prefixes)
            ):
                removed.append(l.key)
                continue
            kept.append(l)
        self.lines = kept
        return removed

    def managed_block(self, begin: str, end: str) -> Optional[Tuple[int, int]]:
        """Index range [i, j) of an existing managed block, markers included."""
        start = None
        for i, l in enumerate(self.lines):
            if l.kind == KIND_COMMENT and l.raw.strip() == begin:
                start = i
            elif start is not None and l.kind == KIND_COMMENT and l.raw.strip() == end:
                return (start, i + 1)
        if start is not None:
            return (start, len(self.lines))      # unterminated: to the end
        return None

    def managed_pairs(self, begin: str, end: str) -> List[Tuple[str, str]]:
        rng = self.managed_block(begin, end)
        if rng is None:
            return []
        return [(l.key, l.value) for l in self.lines[rng[0]:rng[1]] if l.kind == KIND_PAIR]  # type: ignore[misc]

    def replace_managed_block(self, begin: str, end: str, pairs: List[Tuple[str, str]]) -> None:
        rng = self.managed_block(begin, end)
        if rng is not None:
            del self.lines[rng[0]:rng[1]]
            # drop one blank line the block left behind
            if rng[0] > 0 and rng[0] <= len(self.lines) and self.lines[rng[0] - 1].kind == KIND_BLANK:
                if rng[0] == len(self.lines) or self.lines[rng[0]].kind == KIND_BLANK:
                    del self.lines[rng[0] - 1]
        if not pairs:
            return
        if self.lines and self.lines[-1].kind != KIND_BLANK:
            self.lines.append(Line(KIND_BLANK, ""))
        self.lines.append(Line(KIND_COMMENT, begin))
        for k, v in pairs:
            self.lines.append(Line(KIND_PAIR, "%s = %s" % (k, v), key=k, value=v))
        self.lines.append(Line(KIND_COMMENT, end))
        self.trailing_newline = True
