from struct import unpack
from typing import List

from astreum.expression import Expr, NIL, bf16_, FLOAT_TAGS, get_expr_tag, link, str_, symbol
from astreum.machine import OpError


def handle_stack_bf16(machine, stack: List[Expr], env) -> None:
    """Convert value to bf16 (16-bit brain float).
    
    Input types:
    - bytes (2 bytes): decode via LUT
    - str/symbol: parse as float then encode
    - int: convert to float then encode
    - bf16: passthrough
    - other float types: error (strict)
    
    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.
    """
    v = stack.pop()
    tag = get_expr_tag(v)
    
    if tag == "bytes":
        if len(v.value) != 2:
            raise OpError("bf16 requires 2-byte input")
        from astreum.expression.expr import _unpack_u16, _BF16_TABLE
        decoded = _BF16_TABLE[_unpack_u16(v.value)[0]]
        result = bf16_(decoded)
        machine.meter.charge_bytes(v.size())
        stack.append(result)
    elif tag in ("str", "symbol"):
        try:
            parsed = float(v.value)
        except (ValueError, OverflowError):
            raise OpError("bf16: invalid literal")
        try:
            result = bf16_(parsed)
        except ValueError as e:
            raise OpError(str(e))
        machine.meter.charge_bytes(result.size())
        stack.append(result)
    elif tag == "int":
        try:
            parsed = float(v.value)
        except OverflowError:
            raise OpError("bf16: overflow")
        try:
            result = bf16_(parsed)
        except ValueError as e:
            raise OpError(str(e))
        machine.meter.charge_bytes(result.size())
        stack.append(result)
    elif tag == "bf16":
        machine.meter.charge_bytes(v.size())
        stack.append(v)
    else:
        raise OpError(f"bf16 of {tag}")


def handle_stack_bf16_with_result(machine, stack, env):
    """Convert value to bf16 with result wrapping.
    
    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.
    """
    try:
        handle_stack_bf16(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))
