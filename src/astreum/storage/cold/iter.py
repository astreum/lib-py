from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from astreum.storage.cold.merge import _iter_index_entries


def _read_index_ids(index_path: Path, start: int, limit: int) -> tuple[list[bytes], int]:
    """Read up to ``limit`` entry ids from entry number ``start``.

    Returns the ids and the total entry count from the header.

    Raises:
        OSError: If the file is gone or unreadable.
        ValueError: If the file is malformed.
    """
    entry_size = 32 + 64 + 64
    with index_path.open("rb") as index_file:
        count_bytes = index_file.read(64)
        if len(count_bytes) != 64:
            raise ValueError("Invalid index header")
        count = int.from_bytes(count_bytes, "big", signed=False)
        wanted = max(0, min(limit, count - start))
        index_file.seek(64 + start * entry_size)
        ids: list[bytes] = []
        for _ in range(wanted):
            entry = index_file.read(entry_size)
            if len(entry) != entry_size:
                raise ValueError("Invalid index entry")
            ids.append(entry[:32])
    return ids, count


def iter_exprs_in_cold_storage(
    node: Any,
    table: str = "exprs",
    batch: int | None = None,
) -> Iterator[bytes]:
    """Iterate over the content hashes stored in a cold-storage table.

    Yields ids one at a time from ``level_0/*.bin`` filename stems followed by
    every ``level_N/*_index`` entry (N >= 1). The lock is held only for each
    segment read (never across the whole scan), so collate/merge (which
    delete source files) cannot race the listing while other cold reads are
    happening.

    Args:
        node: A Node instance providing ``config`` and ``cold_storage_lock``.
        table: The cold-storage table to iterate (``"exprs"`` or
            ``"records"``). Selects the on-disk subdirectory.
        batch: When set, each index file is read ``batch`` entries at a
            time from a saved offset, with the lock held only for each
            read. A file that is gone at the next read is skipped; a merge
            moved its entries to a higher level, which the scan reaches
            later. When ``None``, a whole index file is read per lock hold.

    Yields:
        The 32-byte content hash of each stored expression.
    """
    from astreum.storage.cold.paths import table_dir

    store_dir = table_dir(node, table)
    if store_dir is None:
        return

    root = Path(store_dir)

    level_0_path = root / "level_0"
    if level_0_path.exists() and level_0_path.is_dir():
        with node.cold_storage_lock:
            stems = [p.stem for p in level_0_path.glob("*.bin")]
        for stem in stems:
            try:
                expr_id = bytes.fromhex(stem)
            except ValueError:
                continue
            if len(expr_id) == 32:
                yield expr_id

    level = 1
    while True:
        level_path = root / f"level_{level}"
        if not level_path.exists() or not level_path.is_dir():
            break
        with node.cold_storage_lock:
            index_paths = sorted(
                (
                    (int(p.name.split("_", 1)[0]), p)
                    for p in level_path.glob("*_index")
                    if p.name.split("_", 1)[0].isdigit()
                ),
                key=lambda item: item[0],
            )
        for _file_number, index_path in index_paths:
            if batch is not None:
                offset = 0
                while True:
                    try:
                        with node.cold_storage_lock:
                            ids, count = _read_index_ids(index_path, offset, batch)
                    except (OSError, ValueError, OverflowError):
                        break
                    yield from ids
                    offset += len(ids)
                    if not ids or offset >= count:
                        break
                continue
            try:
                with node.cold_storage_lock:
                    entries = list(_iter_index_entries(index_path))
            except (OSError, ValueError, OverflowError):
                continue
            for expr_id, _pos, _size in entries:
                yield expr_id
        level += 1
