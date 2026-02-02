import re
from functools import total_ordering
from typing import Any, List, Union


def _split_version(s: str) -> List[Union[int, str]]:
    parts: List[Union[int, str]] = []
    for p in re.split(r"[\.\-_]+", (s or "").strip()):
        if p == "":
            continue
        if p.isdigit():
            parts.append(int(p))
        else:
            parts.append(p.lower())
    return parts


@total_ordering
class LooseVersion:
    def __init__(self, vstring: str = "") -> None:
        self.vstring = vstring
        self.version = _split_version(vstring)

    def __repr__(self) -> str:
        return f"LooseVersion({self.vstring!r})"

    def _cmp_key(self) -> List[Union[int, str]]:
        return self.version

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, LooseVersion):
            return self._cmp_key() == other._cmp_key()
        if isinstance(other, str):
            return self._cmp_key() == LooseVersion(other)._cmp_key()
        return NotImplemented

    def __lt__(self, other: Any) -> bool:
        if isinstance(other, LooseVersion):
            return self._cmp_key() < other._cmp_key()
        if isinstance(other, str):
            return self._cmp_key() < LooseVersion(other)._cmp_key()
        return NotImplemented
