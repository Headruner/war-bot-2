"""Target system resolution: autocomplete (INP-01) and pasted-text parsing (INP-02, INP-03)."""

from __future__ import annotations

import bisect
import re
from urllib.parse import unquote

from .universe import GateGraph

# Links that name a system explicitly win over plain words.
_DOTLAN_SYSTEM = re.compile(r"evemaps\.dotlan\.net/(?:map/[^/\s]+/|system/)([A-Za-z0-9_%\-]+)", re.I)
_DOTLAN_ROUTE = re.compile(r"evemaps\.dotlan\.net/route/(?:\d+:)?([A-Za-z0-9_%:\-]+)", re.I)
_SHOWINFO = re.compile(r"showinfo:5//(\d{8})")
_ZKILL_SYSTEM = re.compile(r"zkillboard\.com/system/(\d{8})", re.I)
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]*")
MAX_WORDS = 3  # longest system names have three words ("Old Man Star")


class TargetResolver:
    def __init__(self, graph: GateGraph):
        self.g = graph
        self._sorted = sorted((n.lower(), i) for i, n in enumerate(graph.names))
        self._keys = [k for k, _ in self._sorted]

    def autocomplete(self, text: str, limit: int = 10) -> list[dict[str, object]]:
        q = text.strip().lower()
        if len(q) < 2:
            return []
        start = bisect.bisect_left(self._keys, q)
        hits: list[int] = []
        for key, idx in self._sorted[start:]:
            if not key.startswith(q) or len(hits) >= limit:
                break
            hits.append(idx)
        if len(hits) < limit:  # then substring matches, shortest names first
            extra = sorted((len(k), idx) for k, idx in self._sorted if q in k and not k.startswith(q))
            hits.extend(idx for _, idx in extra[: limit - len(hits)])
        return [self.g.describe(i) for i in hits]

    def extract(self, text: str) -> list[dict[str, object]]:
        """Candidate systems in order of appearance; explicit links take precedence."""
        linked: list[int] = []
        for m in _DOTLAN_SYSTEM.finditer(text):
            self._add_name(linked, unquote(m.group(1)).replace("_", " "))
        for m in _DOTLAN_ROUTE.finditer(text):
            for part in unquote(m.group(1)).split(":"):
                self._add_name(linked, part.replace("_", " "))
        for rx in (_SHOWINFO, _ZKILL_SYSTEM):
            for m in rx.finditer(text):
                idx = self.g.index.get(int(m.group(1)))
                if idx is not None and idx not in linked:
                    linked.append(idx)
        if linked:
            return [self.g.describe(i) for i in linked]
        words = [(m.group(0), m.start()) for m in _WORD.finditer(_strip_urls(text))]
        found: list[int] = []
        pos = 0
        while pos < len(words):
            matched = 0
            for size in range(MAX_WORDS, 0, -1):  # longest match first
                if pos + size > len(words):
                    continue
                phrase = " ".join(w for w, _ in words[pos : pos + size]).lower()
                idx = self.g.by_name.get(phrase)
                if idx is not None:
                    if idx not in found:
                        found.append(idx)
                    matched = size
                    break
            pos += matched or 1
        return [self.g.describe(i) for i in found]

    def _add_name(self, acc: list[int], name: str) -> None:
        idx = self.g.find(name)
        if idx is not None and idx not in acc:
            acc.append(idx)


def _strip_urls(text: str) -> str:
    return re.sub(r"https?://\S+", " ", text)
