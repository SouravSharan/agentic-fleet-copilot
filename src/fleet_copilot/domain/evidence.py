"""Sequential, deduplicating evidence ledger mapping short handles (`E1`...) to `Evidence`."""

from __future__ import annotations

from typing import Any

from fleet_copilot.domain.models import Evidence


class EvidenceLedger:
    """Assigns deterministic handles (`E1`, `E2`, ...) deduped on `(snapshot_id, metric)`."""

    def __init__(self) -> None:
        self._by_handle: dict[str, Evidence] = {}
        self._key_to_handle: dict[tuple[str, str], str] = {}
        self._next_index: int = 1

    def __len__(self) -> int:
        return len(self._by_handle)

    def add(self, ev: Evidence) -> str:
        """Add an `Evidence` record and return its handle (reusing existing on duplicate key)."""
        key = (ev.snapshot_id, ev.metric)
        existing = self._key_to_handle.get(key)
        if existing is not None:
            return existing

        handle = f"E{self._next_index}"
        self._next_index += 1
        stamped = ev.model_copy(update={"handle": handle})
        self._by_handle[handle] = stamped
        self._key_to_handle[key] = handle
        return handle

    def resolve(self, handle: str) -> Evidence | None:
        """Look up an `Evidence` record by its handle, returning `None` if unknown."""
        return self._by_handle.get(handle)

    def all(self) -> list[Evidence]:
        """Return all registered evidence records in handle order."""
        return list(self._by_handle.values())

    def to_json(self) -> dict[str, dict[str, Any]]:
        """Serialize the ledger as a JSON-native mapping of handle -> evidence dict."""
        return {handle: ev.model_dump(mode="json") for handle, ev in self._by_handle.items()}

    @classmethod
    def from_json(cls, data: dict[str, dict[str, Any]]) -> EvidenceLedger:
        """Reconstruct an `EvidenceLedger` from `to_json()` preserving handles and dedupe index."""
        ledger = cls()

        def _sort_key(item: tuple[str, dict[str, Any]]) -> tuple[int, str]:
            handle = item[0]
            if handle.startswith("E") and handle[1:].isdigit():
                return (int(handle[1:]), handle)
            return (10**9, handle)

        max_idx = 0
        for handle, raw in sorted(data.items(), key=_sort_key):
            ev = Evidence.model_validate(raw)
            if ev.handle != handle:
                ev = ev.model_copy(update={"handle": handle})
            ledger._by_handle[handle] = ev
            ledger._key_to_handle[(ev.snapshot_id, ev.metric)] = handle
            if handle.startswith("E") and handle[1:].isdigit():
                max_idx = max(max_idx, int(handle[1:]))

        ledger._next_index = max_idx + 1
        return ledger
