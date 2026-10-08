
from __future__ import annotations

import os
from pathlib import Path


def _cleanup_temp(*paths: Path) -> None:
    """Remove temporary files if they exist.

    Args:
        *paths: Paths to temporary files to remove.

    Returns: None
    """
    for path in paths:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass


def _fsync_dir(path: Path) -> None:
    """Fsync a directory to ensure durability of file operations.

    Args:
        path: Path to the directory.

    Returns: None
    """
    try:
        dir_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


# index structure
# first 64 bytes is the number of items in the index
# follwed by a concat of the items in the order of the expr hash
# expr hash(32 bytes) | position(64 bytes) | size(64 bytes)

# the collated file structure
# concat of all the binaries


def _next_collated_number(l1_path: Path) -> int:
    """Determine the next available collated file number in level_1.

    Args:
        l1_path: Path to the level_1 directory.

    Returns:
        The next available file number.
    """
    max_number = -1
    for path in l1_path.glob("*_index"):
        prefix = path.name.split("_", 1)[0]
        if prefix.isdigit():
            max_number = max(max_number, int(prefix))
    return max_number + 1


def collate_exprs(store_dir: str | Path) -> bool:
    """Collate all ``level_0`` files into a new ``level_1`` segment.

    Reads every ``level_0/*.bin`` file, sorts entries by content hash, and
    packs them into a new ``level_N``-style segment (``<n>_index`` +
    ``<n>_data``) under ``level_1``, fsyncing each before replacing. Source
    ``level_0`` files are deleted on success.

    Args:
        store_dir: The table directory holding the ``level_0``/``level_N``
            layout (e.g. ``<cold_storage_path>/exprs`` or
            ``<cold_storage_path>/records``).

    Returns:
        ``True`` on success (or when ``level_0`` is empty), ``False`` if the
        input is malformed or an I/O error occurs (temp files are cleaned up).
    """
    level_0_path = Path(store_dir) / "level_0"
    level_1_path = Path(store_dir) / "level_1"

    if not level_0_path.exists() or not level_0_path.is_dir():
        return False

    try:
        level_1_path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    entries = []
    for expr_path in level_0_path.glob("*.bin"):
        expr_hex = expr_path.stem
        try:
            expr_id = bytes.fromhex(expr_hex)
        except ValueError:
            return False
        if len(expr_id) != 32:
            return False
        try:
            expr_size = expr_path.stat().st_size
        except OSError:
            return False
        entries.append((expr_id, expr_path, expr_size))

    if not entries:
        return False

    entries.sort(key=lambda item: item[0])

    expr_position = 0
    index_entries = []
    for expr_id, expr_path, expr_size in entries:
        index_entries.append((expr_id, expr_position, expr_size, expr_path))
        expr_position += expr_size

    file_number = _next_collated_number(level_1_path)
    index_path = level_1_path / f"{file_number}_index"
    data_path = level_1_path / f"{file_number}_data"
    index_tmp_path = level_1_path / f"{file_number}_index.tmp"
    data_tmp_path = level_1_path / f"{file_number}_data.tmp"

    try:
        with index_tmp_path.open("wb") as index_file:
            index_file.write(len(index_entries).to_bytes(64, "big", signed=False))
            for expr_id, position, size, _ in index_entries:
                index_file.write(expr_id)
                index_file.write(position.to_bytes(64, "big", signed=False))
                index_file.write(size.to_bytes(64, "big", signed=False))
            index_file.flush()
            os.fsync(index_file.fileno())
    except (OSError, OverflowError):
        _cleanup_temp(index_tmp_path, data_tmp_path)
        return False

    try:
        with data_tmp_path.open("wb") as data_file:
            for _, _, _, expr_path in index_entries:
                data_file.write(expr_path.read_bytes())
            data_file.flush()
            os.fsync(data_file.fileno())
    except OSError:
        _cleanup_temp(index_tmp_path, data_tmp_path)
        return False

    try:
        os.replace(data_tmp_path, data_path)
        os.replace(index_tmp_path, index_path)
        _fsync_dir(level_1_path)
    except OSError:
        _cleanup_temp(index_tmp_path, data_tmp_path)
        return False

    for _, _, _, expr_path in index_entries:
        try:
            expr_path.unlink()
        except OSError:
            return False

    return True
