import os
import platform
from pathlib import Path


def ensure_data_dir() -> Path:
    """Ensure the data directory exists and return its path.

    Creates Astreum/lib-py with an 'accounts' subdirectory in the appropriate
    platform-specific data directory (APPDATA on Windows, XDG_DATA_HOME or
    ~/.local/share on other platforms).

    Returns:
        Path to the data directory.
    """
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    target = base / "Astreum" / "lib-py"
    for folder in ("accounts",):
        (target / folder).mkdir(parents=True, exist_ok=True)
    return target
