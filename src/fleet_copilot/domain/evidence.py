"""Sequential, deduplicating ledger mapping handles (`E1`, `C1`, `Q1`...) to `Evidence`."""

from __future__ import annotations

from typing import Any, Literal

from fleet_copilot.domain.models import Evidence

HandlePrefix = Literal["E", "C", "Q"]


class EvidenceLedger:
    """Assigns deterministic handles (`E1`, `C1`, `Q1`...) deduped by `(prefix, snap, metric)`."""

    def __init__(self) -> None:
        self._by_handle: dict[str, Evidence] = {}
        self._key_to_handle: dict[tuple[str, str, str], str] = {}
        self._next_by_prefix: dict[str, int] = {"E": 1, "C": 1, "Q": 1}

    def __len__(self) -> int:
        return len(self._by_handle)

    def add(self, ev: Evidence) -> str:
        """Add a telemetry `Evidence` record and return its `E<n>` handle."""
        return self.add_artifact("E", ev)

    def add_artifact(self, prefix: HandlePrefix, ev: Evidence) -> str:
        """Add an evidence/chart/query record under `prefix` (`E`, `C`, or `Q`)."""
        key = (prefix, ev.snapshot_id, ev.metric)
        existing = self._key_to_handle.get(key)
        if existing is not None:
            return existing

        idx = self._next_by_prefix.get(prefix, 1)
        handle = f"{prefix}{idx}"
        self._next_by_prefix[prefix] = idx + 1
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

        def _sort_key(item: tuple[str, dict[str, Any]]) -> tuple[int, int, str]:
            handle = item[0]
            prefix_rank = {"E": 0, "C": 1, "Q": 2}.get(handle[:1], 9)
            if len(handle) >= 2 and handle[:1] in ("E", "C", "Q") and handle[1:].isdigit():
                return (prefix_rank, int(handle[1:]), handle)
            return (9, 10**9, handle)

        max_by_prefix: dict[str, int] = {"E": 0, "C": 0, "Q": 0}
        for handle, raw in sorted(data.items(), key=_sort_key):
            ev = Evidence.model_validate(raw)
            if ev.handle != handle:
                ev = ev.model_copy(update={"handle": handle})
            prefix = handle[:1] if handle[:1] in ("E", "C", "Q") else "E"
            ledger._by_handle[handle] = ev
            ledger._key_to_handle[(prefix, ev.snapshot_id, ev.metric)] = handle
            if len(handle) >= 2 and handle[:1] in max_by_prefix and handle[1:].isdigit():
                max_by_prefix[prefix] = max(max_by_prefix[prefix], int(handle[1:]))

        for pfx, mx in max_by_prefix.items():
            ledger._next_by_prefix[pfx] = mx + 1
        return ledger
