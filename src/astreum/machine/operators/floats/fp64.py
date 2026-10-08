from struct import unpack
from typing import List

from astreum.expression import Expr, NIL, fp64_, FLOAT_TAGS, get_expr_tag, link, str_, symbol
from astreum.machine import OpError


def handle_stack_fp64(machine, stack: List[Expr], env) -> None:
    """Convert value to fp64 (64-bit IEEE 754 float).

    Input types:
    - bytes (8 bytes): struct.unpack
    - str/symbol: parse as float then encode
    - int: convert to float then encode
    - fp64: passthrough
    - other float types: error (strict)

    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.

    Returns: None
    """
    v = stack.pop()
    tag = get_expr_tag(v)
    
    if tag == "bytes":
        if len(v.value) != 8:
            raise OpError("fp64 requires 8-byte input")
        decoded = unpack("<d", v.value)[0]
        try:
            result = fp64_(decoded)
        except ValueError as e:
            raise OpError(str(e))
        machine.meter.charge_bytes(v.size())
        stack.append(result)
    elif tag in ("str", "symbol"):
        try:
            parsed = float(v.value)
        except (ValueError, OverflowError):
            raise OpError("fp64: invalid literal")
        try:
            result = fp64_(parsed)
        except ValueError as e:
            raise OpError(str(e))
        machine.meter.charge_bytes(result.size())
        stack.append(result)
    elif tag == "int":
        try:
            parsed = float(v.value)
        except OverflowError:
            raise OpError("fp64: overflow")
        try:
            result = fp64_(parsed)
        except ValueError as e:
            raise OpError(str(e))
        machine.meter.charge_bytes(result.size())
        stack.append(result)
    elif tag == "fp64":
        machine.meter.charge_bytes(v.size())
        stack.append(v)
    else:
        raise OpError(f"fp64 of {tag}")


def handle_stack_fp64_with_result(machine, stack, env):
    """Convert value to fp64 with result wrapping.

    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.

    Returns: None
    """
    try:
        handle_stack_fp64(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))
