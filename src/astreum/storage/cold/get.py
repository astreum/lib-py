from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from astreum.storage.cold.find import find_expr_in_index
from astreum.expression.encoding import decode_expr_from_bytes


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

            index_files: list[tuple[int, Path]] = []
            for index_path in level_path.glob("*_index"):
                prefix = index_path.name.split("_", 1)[0]
                if prefix.isdigit():
                    index_files.append((int(prefix), index_path))

            index_files.sort(key=lambda item: item[0], reverse=True)

            for file_number, index_path in index_files:
                result = find_expr_in_index(index_path, expr_id)
                if result is None:
                    continue
                pos_bytes, size_bytes = result
                position = int.from_bytes(pos_bytes, "big", signed=False)
                size = int.from_bytes(size_bytes, "big", signed=False)
                data_path = level_path / f"{file_number}_data"
                try:
                    with data_path.open("rb") as data_file:
                        data_file.seek(position)
                        data = data_file.read(size)
                except OSError:
                    return None
                if len(data) != size:
                    return None
                try:
                    return decode_expr_from_bytes(data)
                except ValueError:
                    return None

            level += 1

        return None
