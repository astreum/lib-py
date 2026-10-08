"""Parsing and formatting of human-readable Astreum currency amounts.

Amounts on-chain are plain integers of the smallest base unit. This module
converts human notation such as ``"1 000 000"``, ``"1MA"``, ``"1 MA"``,
``"1 MegaAstre"`` or ``"1.5k"`` into exact integer amounts and back. SI
prefixes from kilo through Quetta are supported.
"""

from decimal import Decimal, localcontext
import re


_PREFIXES: dict[str, int] = {
    "k": 10**3,
    "m": 10**6,
    "g": 10**9,
    "t": 10**12,
    "p": 10**15,
    "e": 10**18,
    "z": 10**21,
    "y": 10**24,
    "r": 10**27,
    "q": 10**30,
}

_FULL_NAMES: dict[str, str] = {
    "k": "kilo",
    "m": "Mega",
    "g": "Giga",
    "t": "Tera",
    "p": "Peta",
    "e": "Exa",
    "z": "Zetta",
    "y": "Yotta",
    "r": "Ronna",
    "q": "Quetta",
}

_DISPLAY_PREFIXES: dict[str, str] = {
    "k": "k",
    "m": "M",
    "g": "G",
    "t": "T",
    "p": "P",
    "e": "E",
    "z": "Z",
    "y": "Y",
    "r": "R",
    "q": "Q",
}

_UNITS: dict[str, int] = {"": 1, "a": 1, "astre": 1}
for _prefix, _factor in _PREFIXES.items():
    _UNITS[_prefix + "a"] = _factor
    _UNITS[_prefix + "astre"] = _factor
    _UNITS[_prefix] = _factor
    _UNITS[_FULL_NAMES[_prefix].lower() + "astre"] = _factor

_NUMBER_RE = re.compile(
    r"^\s*"
    r"(?P<coefficient>[+-]?(?:(?:\d{1,3}(?: \d{3})+|\d+)(?:\.\d*)?|\.\d+))"
    r"\s*(?P<unit>[A-Za-z]*)"
    r"\s*$"
)


def parse_astre_amount(text: str) -> int:
    """Convert human-readable text into an exact integer amount of base units.

    Numeric notation is decimal only: plain integers (``"1000"``), decimals
    (``"1.5"``) and SI-style digit grouping with single spaces in the
    integer part (``"1 000 000"``. Groups after the first must be exactly
    three digits). Scientific notation is not accepted, so ``e``/``E``
    always means the Exa prefix. Unit suffixes are case-insensitive and
    space-agnostic: ``"1MA"``, ``"1 MA"``, ``"1 mastre"``, ``"1 MegaAstre"``,
    ``"1.5k"``, ``"2 T"``. SI prefixes run from kilo (10^3) through
    Quetta (10^30).

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

    coefficient = Decimal(match.group("coefficient").replace(" ", ""))

    unit = match.group("unit").lower()
    if unit not in _UNITS:
        raise ValueError(f"unknown unit in amount: {text!r}")

    with localcontext() as context:
        context.prec = 60
        value = coefficient * _UNITS[unit]
        if value != value.to_integral_value():
            raise ValueError(f"amount does not resolve to whole base units: {text!r}")
        return int(value)


def format_astre_amount(amount: int, style: str = "short") -> str:
    """Format an integer amount of base units as human-readable text.

    The largest prefix for which the value is >= 1 is used. Trailing zeros
    are trimmed. Round-trip is guaranteed:
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

    best_prefix = ""
    for prefix in ("q", "r", "y", "z", "e", "p", "t", "g", "m", "k"):
        if amount >= _PREFIXES[prefix]:
            best_prefix = prefix
            break

    scaled = Decimal(amount) / _PREFIXES[best_prefix]
    text = format(scaled.normalize(), "f")
    if style == "full":
        text += " " + _FULL_NAMES[best_prefix] + "Astre"
    else:
        text += " " + _DISPLAY_PREFIXES[best_prefix] + "A"
    return text
