from struct import unpack
from typing import List

from astreum.expression import Expr, NIL, e5m2_, FLOAT_TAGS, get_expr_tag, link, str_, symbol
from astreum.machine import OpError


def handle_stack_e5m2(machine, stack: List[Expr], env) -> None:
    """Convert value to e5m2 (8-bit float with 5-bit exponent, 2-bit mantissa).
    
    Input types:
    - bytes (1 byte): decode via LUT
    - str/symbol: parse as float then encode
    - int: convert to float then encode
    - e5m2: passthrough
    - other float types: error (strict)
    
    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.
    """
    v = stack.pop()
    tag = get_expr_tag(v)
    
    if tag == "bytes":
        if len(v.value) != 1:
            raise OpError("e5m2 requires 1-byte input")
        from astreum.expression.expr import _E5M2_TABLE
        decoded = _E5M2_TABLE[v.value[0]]
        result = e5m2_(decoded)
        machine.meter.charge_bytes(v.size())
        stack.append(result)
    elif tag in ("str", "symbol"):
        try:
            parsed = float(v.value)
        except (ValueError, OverflowError):
            raise OpError("e5m2: invalid literal")
        try:
            result = e5m2_(parsed)
        except ValueError as e:
            raise OpError(str(e))
        machine.meter.charge_bytes(result.size())
        stack.append(result)
    elif tag == "int":
        try:
            parsed = float(v.value)
        except OverflowError:
            raise OpError("e5m2: overflow")
        try:
            result = e5m2_(parsed)
        except ValueError as e:
            raise OpError(str(e))
        machine.meter.charge_bytes(result.size())
        stack.append(result)
    elif tag == "e5m2":
        machine.meter.charge_bytes(v.size())
        stack.append(v)
    else:
        raise OpError(f"e5m2 of {tag}")


def handle_stack_e5m2_with_result(machine, stack, env):
    """Convert value to e5m2 with result wrapping.
    
    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.
    """
    try:
        handle_stack_e5m2(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))
