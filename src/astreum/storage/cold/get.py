from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

from astreum.storage.cold.find import find_expr_in_index, find_exprs_in_index
from astreum.expression.encoding import decode_expr_from_bytes


def _level_index_files(level_path: Path) -> list[tuple[int, Path]]:
    index_files: list[tuple[int, Path]] = []
    for index_path in level_path.glob("*_index"):
        prefix = index_path.name.split("_", 1)[0]
        if prefix.isdigit():
            index_files.append((int(prefix), index_path))
    index_files.sort(key=lambda item: item[0], reverse=True)
    return index_files


def _read_data_spans(data_path: Path, spans: list[tuple[int, int]]) -> list[Optional[bytes]]:
    results: list[Optional[bytes]] = []
    with data_path.open("rb") as data_file:
        for position, size in spans:
            data_file.seek(position)
            data = data_file.read(size)
            results.append(data if len(data) == size else None)
    return results


def get_expr_from_cold_storage(
    node: Any,
    expr_id: bytes,
    table: str = "exprs",
) -> Optional["Expr"]:
    """Retrieve an Expr from cold storage by content hash.

    Searches the table's ``level_0/*.bin`` first, then walks ``level_N``
    (ascending) index files in reverse file order (highest number first,
    which holds the most recent data for a key). Returns the first hit,
    decoded from bytes. Runs under ``cold_storage_lock`` so collate/merge
    cannot remove source files mid-read.

    Args:
        node: A Node instance providing ``config`` and ``cold_storage_lock``.
        expr_id: The 32-byte content hash of the expression to retrieve.
        table: The cold-storage table to read from (``"exprs"`` or
            ``"records"``). Selects the on-disk subdirectory.

    Returns:
        The decoded Expr, or ``None`` if not found or the data is malformed.
    """
    from astreum.expression import Expr
    from astreum.storage.cold.paths import table_dir

    store_dir = table_dir(node, table)
    if store_dir is None:
        return None
    with node.cold_storage_lock:
        level_0_path = store_dir / "level_0"
        if level_0_path.exists() and level_0_path.is_dir():
            key_hex = expr_id.hex().upper()
            expr_path = level_0_path / f"{key_hex}.bin"
            try:
                data = expr_path.read_bytes()
                return decode_expr_from_bytes(data)
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                return None

        level = 1
        while True:
            level_path = store_dir / f"level_{level}"
            if not level_path.exists() or not level_path.is_dir():
                break

            index_files = _level_index_files(level_path)

            for file_number, index_path in index_files:
                result = find_expr_in_index(index_path, expr_id)
                if result is None:
                    continue
                pos_bytes, size_bytes = result
                position = int.from_bytes(pos_bytes, "big", signed=False)
                size = int.from_bytes(size_bytes, "big", signed=False)
                data_path = level_path / f"{file_number}_data"
                try:
                    data = _read_data_spans(data_path, [(position, size)])[0]
                except OSError:
                    return None
                if data is None:
                    return None
                try:
                    return decode_expr_from_bytes(data)
                except ValueError:
                    return None

            level += 1

        return None


def get_exprs_from_cold_storage(
    node: Any,
    expr_ids: Iterable[bytes],
    table: str = "exprs",
) -> dict[bytes, "Expr"]:
    """Retrieve many Exprs from cold storage under one lock acquisition.

    Uses the same search order as ``get_expr_from_cold_storage``: ``level_0``
    first, then each ``level_N`` ascending with the highest file number
    first. Each index file is opened once, and a found key is not searched
    in later files. Each data file is opened once and its hits are read in
    position order.

    Args:
        node: A Node instance providing ``config`` and ``cold_storage_lock``.
        expr_ids: The 32-byte content hashes to retrieve.
        table: The cold-storage table to read from (``"exprs"`` or
            ``"records"``).

    Returns:
        A map holding only the keys that are found. A key whose data is
        malformed is left out.
    """
    from astreum.storage.cold.paths import table_dir

    store_dir = table_dir(node, table)
    if store_dir is None:
        return {}
    pending = set(expr_ids)
    results: dict[bytes, "Expr"] = {}
    with node.cold_storage_lock:
        level_0_path = store_dir / "level_0"
        if pending and level_0_path.is_dir():
            for key in list(pending):
                try:
                    data = (level_0_path / f"{key.hex().upper()}.bin").read_bytes()
                except FileNotFoundError:
                    continue
                except OSError:
                    pending.discard(key)
                    continue
                pending.discard(key)
                try:
                    results[key] = decode_expr_from_bytes(data)
                except ValueError:
                    pass

        level = 1
        while pending:
            level_path = store_dir / f"level_{level}"
            if not level_path.is_dir():
                break
            for file_number, index_path in _level_index_files(level_path):
                if not pending:
                    break
                hits = find_exprs_in_index(index_path, pending)
                if not hits:
                    continue
                pending.difference_update(hits)
                by_position = sorted(hits.items(), key=lambda item: item[1][0])
                try:
                    blobs = _read_data_spans(
                        level_path / f"{file_number}_data",
                        [span for _key, span in by_position],
                    )
                except OSError:
                    continue
                for (key, _span), data in zip(by_position, blobs):
                    if data is None:
                        continue
                    try:
                        results[key] = decode_expr_from_bytes(data)
                    except ValueError:
                        pass
            level += 1
    return results
