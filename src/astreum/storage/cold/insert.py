from __future__ import annotations

from pathlib import Path
from typing import Any

from astreum.storage.cold.collate import collate_exprs
from astreum.storage.cold.merge import merge_exprs
from astreum.expression.encoding import encode_expr_to_bytes


def _level_size(level_path: Path) -> int | None:
    """Sum the sizes of all files in a level directory.

    Args:
        level_path: Path to the level directory.

    Returns:
        Total size in bytes, or None on error.
    """
    total = 0
    for entry in level_path.iterdir():
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            return None
    return total


def _level_limit(node: Any, level: int) -> int:
    """Return the size limit for a given level (base × 10^level).

    Args:
        node: A Node instance providing config.
        level: The level number (>= 0).

    Returns:
        The size limit in bytes for that level.

    Raises:
        ValueError: If cold_storage_base_size is missing or invalid.
    """
    try:
        base_limit = int(node.config["cold_storage_base_size"])
    except (TypeError, ValueError) as exc:
        raise ValueError("cold_storage_base_size must be an integer") from exc
    if base_limit <= 0:
        raise ValueError("cold_storage_base_size must be positive")
    return base_limit * (10 ** level)


def put_expr_in_cold_storage(
    node: Any,
    expr: "Expr",
    table: str = "exprs",
    size_attr: str = "cold_storage_level_0_size",
    key: bytes | None = None,
) -> bool:
    """Write an Expr into cold storage with automatic collation and merging.

    Stores the expression bytes to the table's ``level_0`` under its content
    hash. If ``level_0`` exceeds ``cold_storage_base_size``, triggers a
    collation step that packs entries into higher levels. Higher
    levels that exceed their size limit (``base_size × 10^level``)
    are merged recursively.

    Link children are stored before their parent to guarantee that
    all dependencies are on disk before the parent reference.

    Args:
        node: A Node instance providing config and storage access.
        expr: The expression to persist.
        table: The cold-storage table to write into (``"exprs"`` or
            ``"records"``). Selects the on-disk subdirectory.
        size_attr: Node attribute tracking the table's current
            ``level_0`` size.
        key: Optional explicit key (file stem id) instead of ``expr.hash()``.

    Returns:
        True on success, False if storage is misconfigured or an
        I/O error occurs.
    """
    # Descend into Link children first so they're stored before the parent
    if expr.base == "link":
        if expr._head is not None:
            put_expr_in_cold_storage(node, expr._head, table=table, size_attr=size_attr, key=key)
        if expr._tail is not None:
            put_expr_in_cold_storage(node, expr._tail, table=table, size_attr=size_attr, key=key)
        # For builtin composites (int, str, float), the encoded value must be stored
        # as a separate bytes expression so head_hash can be resolved later.
        if expr._head is None and expr.value is not None and expr._tail is not None and expr._tail.base == "symbol":
            from astreum.expression.expr import TYPE_SYMBOLS, TAG_BYTE_ENCODINGS, Expr
            type_name = expr._tail.value
            if type_name in TYPE_SYMBOLS:
                encoder = TAG_BYTE_ENCODINGS.get(type_name)
                if encoder is not None:
                    encoded = encoder(expr.value)
                    value_expr = Expr("bytes", value=encoded)
                    put_expr_in_cold_storage(node, value_expr, table=table, size_attr=size_attr, key=key)

    expr_id = key if key is not None else expr.hash()
    expr_bytes = encode_expr_to_bytes(expr)

    from astreum.storage.cold.paths import table_dir

    store_dir = table_dir(node, table)
    if store_dir is None:
        return False
    level_0_path = store_dir / "level_0"

    with node.cold_storage_lock:
        expr_path = level_0_path / f"{expr_id.hex().upper()}.bin"
        try:
            expr_path.write_bytes(expr_bytes)
        except OSError:
            return False

        size = getattr(node, size_attr, 0)
        size += len(expr_bytes)
        setattr(node, size_attr, size)

        if size > node.config["cold_storage_base_size"]:
            if not collate_exprs(store_dir):
                return False
            setattr(node, size_attr, 0)

            level = 1
            while True:
                level_path = store_dir / f"level_{level}"
                if not level_path.exists() or not level_path.is_dir():
                    break

                level_bytes = _level_size(level_path)
                if level_bytes is None:
                    return False
                try:
                    level_limit = _level_limit(node, level)
                except ValueError:
                    return False
                if level_bytes > level_limit:
                    if not merge_exprs(store_dir, level):
                        return False

                level += 1

    return True
