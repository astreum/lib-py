from struct import unpack
from typing import List

from astreum.expression import Expr, NIL, fp32_, FLOAT_TAGS, get_expr_tag, link, str_, symbol
from astreum.machine import OpError


def handle_stack_fp32(machine, stack: List[Expr], env) -> None:
    """Convert value to fp32 (32-bit IEEE 754 float).

    Input types:
    - bytes (4 bytes): struct.unpack
    - str/symbol: parse as float then encode
    - int: convert to float then encode
    - fp32: passthrough
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
        if len(v.value) != 4:
            raise OpError("fp32 requires 4-byte input")
        decoded = unpack('<f', v.value)[0]
        try:
            result = fp32_(decoded)
        except ValueError as e:
            raise OpError(str(e))
        machine.meter.charge_bytes(v.size())
        stack.append(result)
    elif tag in ("str", "symbol"):
        try:
            parsed = float(v.value)
        except (ValueError, OverflowError):
            raise OpError("fp32: invalid literal")
        try:
            result = fp32_(parsed)
        except ValueError as e:
            raise OpError(str(e))
        machine.meter.charge_bytes(result.size())
        stack.append(result)
    elif tag == "int":
        try:
            parsed = float(v.value)
        except OverflowError:
            raise OpError("fp32: overflow")
        try:
            result = fp32_(parsed)
        except ValueError as e:
            raise OpError(str(e))
        machine.meter.charge_bytes(result.size())
        stack.append(result)
    elif tag == "fp32":
        machine.meter.charge_bytes(v.size())
        stack.append(v)
    else:
        raise OpError(f"fp32 of {tag}")


def handle_stack_fp32_with_result(machine, stack, env):
    """Convert value to fp32 with result wrapping.

    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.

    Returns: None
    """
    try:
        handle_stack_fp32(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))
