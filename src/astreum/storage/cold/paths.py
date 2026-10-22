from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


def table_dir(node: Any, table: str) -> Optional[Path]:
    """Return the on-disk directory for a cold-storage table.

    The cold store root is ``node.config.cold_storage_path``. Each table
    (``"exprs"``, ``"records"``) lives in its own subdirectory holding the
    ``level_0``/``level_N`` layout.

    Args:
        node: A Node instance providing ``config``.
        table: The table name, matching its directory name.

    Returns:
        The table directory, or ``None`` when cold storage is disabled.
    """
    root = node.config.cold_storage_path
    if not root:
        return None
    return Path(root) / table
