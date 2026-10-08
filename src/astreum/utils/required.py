from __future__ import annotations

from typing import Iterable


def required_fields(instance: object, field_names: Iterable[str]) -> None:
    """Raise ValueError if any named fields are missing or set to None.

    Args:
        instance: Object instance to check.
        field_names: Iterable of attribute names to validate.

    Returns: None

    Raises:
        ValueError: If any fields are missing or None.
    """
    missing = []
    for name in field_names:
        if not hasattr(instance, name) or getattr(instance, name) is None:
            missing.append(name)
    if missing:
        class_name = instance.__class__.__name__
        raise ValueError(f"{class_name} missing required fields: {', '.join(missing)}")
