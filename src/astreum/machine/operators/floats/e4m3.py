from struct import unpack
from typing import List

from astreum.expression import Expr, NIL, e4m3_, FLOAT_TAGS, _expr_to_fp64, get_expr_tag, link, str_, symbol
from astreum.machine import OpError


def handle_stack_e4m3(machine, stack: List[Expr], env) -> None:
    """Convert value to e4m3 (8-bit float with 4-bit exponent, 3-bit mantissa).
    
    Input types:
    - bytes (1 byte): decode via LUT
    - str/symbol: parse as float then encode
    - int: convert to float then encode
    - e4m3: passthrough
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
            raise OpError("e4m3 requires 1-byte input")
        from astreum.expression.expr import _E4M3_TABLE
        decoded = _E4M3_TABLE[v.value[0]]
        result = e4m3_(decoded)
        machine.meter.charge_bytes(v.size())
        stack.append(result)
    elif tag in ("str", "symbol"):
        try:
            parsed = float(v.value)
        except (ValueError, OverflowError):
            raise OpError("e4m3: invalid literal")
        try:
            result = e4m3_(parsed)
        except ValueError as e:
            raise OpError(str(e))
        machine.meter.charge_bytes(result.size())
        stack.append(result)
    elif tag == "int":
        try:
            parsed = float(v.value)
        except OverflowError:
            raise OpError("e4m3: overflow")
        try:
            result = e4m3_(parsed)
        except ValueError as e:
            raise OpError(str(e))
        machine.meter.charge_bytes(result.size())
        stack.append(result)
    elif tag == "e4m3":
        machine.meter.charge_bytes(v.size())
        stack.append(v)
    else:
        raise OpError(f"e4m3 of {tag}")


def handle_stack_e4m3_with_result(machine, stack, env):
    """Convert value to e4m3 with result wrapping.
    
    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.
    """
    try:
        handle_stack_e4m3(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))
