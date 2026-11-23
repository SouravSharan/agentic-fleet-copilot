"""OS version parsing and same-family comparison for macOS and Windows."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

OsFamily = Literal["macos", "windows"]

_MACOS_RE = re.compile(r"^(?:macos\s+)?(\d+)(?:\.(\d+))?(?:\.(\d+))?$", re.IGNORECASE)
_WINDOWS_RE = re.compile(
    r"^(?:windows\s+)?(\d+)(?:\s+(\d{2})H([12]))?$",
    re.IGNORECASE,
)


def _pad_pair(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)), b + (0,) * (n - len(b))


class OsVersion(BaseModel):
    """Comparable parsed OS version tagged with its OS family."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    family: OsFamily
    parts: tuple[int, ...]
    raw: str

    def _check_same_family(self, other: object) -> OsVersion:
        if not isinstance(other, OsVersion):
            return NotImplemented  # type: ignore[return-value]
        if self.family != other.family:
            raise TypeError(
                f"Cannot compare OS versions across families: {self.family} vs {other.family}"
            )
        return other

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, OsVersion):
            return False
        if self.family != other.family:
            return False
        a, b = _pad_pair(self.parts, other.parts)
        return a == b

    def __lt__(self, other: object) -> bool:
        target = self._check_same_family(other)
        a, b = _pad_pair(self.parts, target.parts)
        return a < b

    def __le__(self, other: object) -> bool:
        target = self._check_same_family(other)
        a, b = _pad_pair(self.parts, target.parts)
        return a <= b

    def __gt__(self, other: object) -> bool:
        target = self._check_same_family(other)
        a, b = _pad_pair(self.parts, target.parts)
        return a > b

    def __ge__(self, other: object) -> bool:
        target = self._check_same_family(other)
        a, b = _pad_pair(self.parts, target.parts)
        return a >= b


def _infer_family(product_name: str | None) -> OsFamily | None:
    if not product_name or not product_name.strip():
        return None
    norm = product_name.strip().lower()
    if norm in ("macos", "mac os", "mac os x", "darwin") or norm.startswith("macos"):
        return "macos"
    if norm == "win32" or "windows" in norm:
        return "windows"
    return None


def parse_version_for_family(family: OsFamily, version_str: str | None) -> OsVersion | None:
    """Parse a version string when the OS family is already known."""
    if not version_str or not version_str.strip():
        return None
    raw = version_str.strip()

    if family == "macos":
        m = _MACOS_RE.match(raw)
        if not m:
            return None
        major = int(m.group(1))
        minor = int(m.group(2)) if m.group(2) is not None else 0
        if m.group(3) is not None:
            parts: tuple[int, ...] = (major, minor, int(m.group(3)))
        else:
            parts = (major, minor)
        return OsVersion(family="macos", parts=parts, raw=raw)

    if family == "windows":
        m = _WINDOWS_RE.match(raw)
        if not m:
            return None
        major = int(m.group(1))
        if m.group(2) is not None and m.group(3) is not None:
            parts = (major, int(m.group(2)), int(m.group(3)))
        else:
            parts = (major, 0, 0)
        return OsVersion(family="windows", parts=parts, raw=raw)

    return None


def parse_os(product_name: str | None, product_version: str | None) -> OsVersion | None:
    """Parse OS product name and version into a structured `OsVersion`, or `None` if unknown."""
    family = _infer_family(product_name)
    if family is None:
        return None
    return parse_version_for_family(family, product_version)


def older_than(v: OsVersion | None, family: OsFamily, threshold: str) -> bool | None:
    """Return True if `v` is in `family` and strictly older than `threshold`, else False/None.

    Returns `None` across different OS families or if `v` or `threshold` cannot be parsed.
    """
    if v is None or v.family != family:
        return None
    target = parse_version_for_family(family, threshold)
    if target is None:
        return None
    return v < target
