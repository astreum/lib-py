"""Parsing and formatting of human-readable Astreum currency amounts.

Amounts on-chain are plain integers of the smallest base unit. This module
converts human notation such as ``"1 000 000"``, ``"1MA"``, ``"1 MA"``,
``"1 MegaAstre"``, ``"1.5k"`` or ``"5.0284e31"`` into exact integer amounts
and back.

SI scale symbols are case-sensitive. ``k`` is lowercase. The other symbols
are uppercase: ``M``, ``G``, ``T``, ``P``, ``E``, ``Z``, ``Y``, ``R`` and
``Q``. Lowercase scale aliases are not accepted. Full names such as
``"MegaAstre"`` are case-insensitive.

A lowercase ``e`` is a scientific-notation exponent. An uppercase ``E`` is
the Exa scale symbol. Thus ``"1e6"`` is one million base units, and ``"1E"``
is one ExaAstre.

Amounts are exact integers. Do not use a Python float. A binary float cannot
represent a large integer exactly.
"""

import re


_PREFIXES: dict[str, int] = {
    "k": 10**3,
    "M": 10**6,
    "G": 10**9,
    "T": 10**12,
    "P": 10**15,
    "E": 10**18,
    "Z": 10**21,
    "Y": 10**24,
    "R": 10**27,
    "Q": 10**30,
}

_FULL_NAMES: dict[str, str] = {
    "k": "kilo",
    "M": "Mega",
    "G": "Giga",
    "T": "Tera",
    "P": "Peta",
    "E": "Exa",
    "Z": "Zetta",
    "Y": "Yotta",
    "R": "Ronna",
    "Q": "Quetta",
}

# Full unit names, lowercased. The base unit is included.
_FULL_UNITS: dict[str, int] = {
    "": 1,
    "a": 1,
    "astre": 1,
}
for _symbol, _name in _FULL_NAMES.items():
    _FULL_UNITS[_name.lower() + "astre"] = _PREFIXES[_symbol]


_NUMBER_RE = re.compile(
    r"^\s*"
    r"(?P<coefficient>[+-]?(?:(?:\d{1,3}(?: \d{3})+|\d+)(?:\.\d*)?|\.\d+))"
    r"(?P<exponent>e[+-]?\d+)?"
    r"\s*(?P<unit>[A-Za-z]*)"
    r"\s*$"
)


def _unit_factor(unit: str) -> int:
    """Return the multiplier from a unit suffix to base units.

    Args:
        unit: The unit suffix as written.

    Returns:
        The multiplier from the unit to base units.

    Raises:
        ValueError: If the unit is not a known scale symbol or full name.
    """
    lower = unit.lower()
    if lower in _FULL_UNITS:
        return _FULL_UNITS[lower]

    if unit and unit[0] in _PREFIXES:
        suffix = unit[1:].lower()
        if suffix in ("", "a", "astre"):
            return _PREFIXES[unit[0]]

    raise ValueError(f"unknown unit in amount: {unit!r}")


def parse_astre_amount(text: str) -> int:
    """Convert human-readable text into an exact integer amount of base units.

    Numeric notation is decimal only. It accepts plain integers
    (``"1000"``), decimals (``"1.5"``), SI-style digit grouping with single
    spaces in the integer part (``"1 000 000"``; groups after the first must
    be exactly three digits), and a lowercase ``e`` exponent attached to the
    coefficient (``"1e6"``, ``"5.0284e31"``, ``"1e-1 kA"``). The exponent
    applies before the unit.

    Uppercase ``E`` is the Exa scale symbol, never an exponent. Scale symbols
    are case-sensitive: ``k`` is lowercase, the rest are uppercase. Full
    names such as ``"MegaAstre"`` are case-insensitive.

    Args:
        text: Human-readable amount notation.

    Returns:
        The exact integer amount in base units.

    Raises:
        ValueError: If ``text`` is not a string, is not valid amount
            notation, or does not resolve to a whole number of base units.
    """
    if not isinstance(text, str):
        raise ValueError(f"amount must be a string, got {type(text).__name__}")

    match = _NUMBER_RE.match(text)
    if match is None:
        raise ValueError(f"invalid amount: {text!r}")

    coefficient = match.group("coefficient").replace(" ", "")
    unit = match.group("unit")
    factor = _unit_factor(unit)

    exponent_text = match.group("exponent")
    exponent = int(exponent_text[1:]) if exponent_text else 0

    sign = -1 if coefficient.startswith("-") else 1
    if coefficient[0] in "+-":
        coefficient = coefficient[1:]

    if "." in coefficient:
        whole, fraction = coefficient.split(".", 1)
    else:
        whole, fraction = coefficient, ""

    mantissa = int((whole or "0") + fraction)
    scale = len(fraction) - exponent
    value = mantissa * factor

    if scale > 0:
        divisor = 10**scale
        if value % divisor:
            raise ValueError(
                f"amount does not resolve to whole base units: {text!r}"
            )
        value //= divisor
    elif scale < 0:
        value *= 10 ** (-scale)

    return sign * value


def format_astre_amount(amount: int, style: str = "short") -> str:
    """Format an integer amount of base units as human-readable text.

    Use the largest prefix for which the value is >= 1. Trim trailing zeros.
    Round-trip is guaranteed:
    ``parse_astre_amount(format_astre_amount(n)) == n``.

    Args:
        amount: Integer amount in base units.
        style: Output style. ``"short"`` uses the SI value-space-symbol
            layout with prefix letters (``"1 kA"``, ``"1.5 MA"``) and a
            bare unit below 1000 (``"999 A"``). ``"full"`` uses full
            unit names (``"1 kiloAstre"``, ``"999 Astre"``). Defaults to
            ``"short"``.

    Returns:
        The human-readable representation of the amount.

    Raises:
        ValueError: If ``amount`` is not an int or ``style`` is not
            ``"short"`` or ``"full"``.
    """
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise ValueError(f"amount must be an int, got {type(amount).__name__}")

    if style not in ("short", "full"):
        raise ValueError(f"unknown style: {style!r}")

    if amount < 0:
        return "-" + format_astre_amount(-amount, style)

    if amount < 1000:
        return f"{amount} Astre" if style == "full" else f"{amount} A"

    best_symbol = ""
    for symbol in ("Q", "R", "Y", "Z", "E", "P", "T", "G", "M", "k"):
        if amount >= _PREFIXES[symbol]:
            best_symbol = symbol
            break

    factor = _PREFIXES[best_symbol]
    whole, remainder = divmod(amount, factor)
    if remainder == 0:
        text = str(whole)
    else:
        width = len(str(factor)) - 1
        fraction = str(remainder).rjust(width, "0").rstrip("0")
        text = f"{whole}.{fraction}"

    if style == "full":
        text += " " + _FULL_NAMES[best_symbol] + "Astre"
    else:
        text += " " + best_symbol + "A"
    return text
