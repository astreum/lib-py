from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional


def find_expr_in_index(index_filepath: str | Path, key: bytes) -> Optional[tuple[bytes, bytes]]:
    """Binary-search an index file for a key and return its data location.

    Index files are sorted by key.  Each entry is 160 bytes: a 32-byte
    content hash, a 64-byte data position, and a 64-byte size, preceded by a
    64-byte big-endian entry count header.

    Args:
        index_filepath: Path to a ``*_index`` file.
        key: The 32-byte content hash to look up.

    Returns:
        A ``(position_bytes, size_bytes)`` tuple (each a 64-byte big-endian
        integer) describing where the value lives in the sibling ``*_data``
        file, or ``None`` if the key is absent or the file is malformed.
    """
    if len(key) != 32:
        return None

    index_path = Path(index_filepath)
    try:
        with index_path.open("rb") as index_file:
            count = _read_index_count(index_file)
            hit = _search_open_index(index_file, count, key)
    except (OSError, ValueError):
        return None
    return hit


def find_exprs_in_index(
    index_filepath: str | Path,
    keys: Iterable[bytes],
) -> dict[bytes, tuple[int, int]]:
    """Binary-search an index file for many keys with a single open.

    Args:
        index_filepath: Path to a ``*_index`` file.
        keys: The 32-byte content hashes to look up. They are sorted
            before the search; keys of another length are ignored.

    Returns:
        A ``key -> (position, size)`` map holding only the keys that are
        found. A malformed file ends the search and keeps the hits so far.
    """
    sorted_keys = sorted({key for key in keys if len(key) == 32})
    hits: dict[bytes, tuple[int, int]] = {}
    if not sorted_keys:
        return hits
    try:
        with Path(index_filepath).open("rb") as index_file:
            count = _read_index_count(index_file)
            for key in sorted_keys:
                hit = _search_open_index(index_file, count, key)
                if hit is not None:
                    hits[key] = (
                        int.from_bytes(hit[0], "big", signed=False),
                        int.from_bytes(hit[1], "big", signed=False),
                    )
    except (OSError, ValueError):
        pass
    return hits


def _read_index_count(index_file) -> int:
    count_bytes = index_file.read(64)
    if len(count_bytes) != 64:
        raise ValueError("Invalid index header")
    return int.from_bytes(count_bytes, "big", signed=False)


def _search_open_index(index_file, count: int, key: bytes) -> Optional[tuple[bytes, bytes]]:
    entry_size = 32 + 64 + 64
    left = 0
    right = count - 1
    while left <= right:
        mid = (left + right) // 2
        index_file.seek(64 + (mid * entry_size))
        expr_id = index_file.read(32)
        pos_bytes = index_file.read(64)
        size_bytes = index_file.read(64)
        if len(expr_id) != 32 or len(pos_bytes) != 64 or len(size_bytes) != 64:
            raise ValueError("Invalid index entry")
        if expr_id == key:
            return pos_bytes, size_bytes
        if expr_id < key:
            left = mid + 1
        else:
            right = mid - 1
    return None
