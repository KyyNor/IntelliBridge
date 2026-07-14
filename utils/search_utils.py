"""Shared helpers for case-insensitive text indexes."""

from typing import Dict, List, Set


def lookup_text_index(filter_text: str, index: Dict[str, List[str]]) -> Set[str]:
    """Return values whose normalized index key contains the filter text."""

    normalized = (filter_text or "").strip().lower()
    if not normalized:
        return set()

    return {
        value
        for key, values in index.items()
        if normalized in key.lower()
        for value in values
    }
