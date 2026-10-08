from typing import List

from astreum.expression import Expr, NIL, get_expr_tag, int_, FLOAT_TAGS, _expr_to_fp64, link, str_, symbol
from astreum.machine import OpError


def handle_stack_int(machine, stack: List[Expr], env) -> None:
    """Convert value to integer.
    
    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.
    """
    v = stack.pop()
    tag = get_expr_tag(v)
    if tag == "bytes":
        result = int_(int.from_bytes(v.value, "little", signed=True))
        machine.meter.charge_bytes(v.size())
        stack.append(result)
    elif tag in ("str", "symbol"):
        try:
            result = int_(int(v.value))
        except ValueError:
            raise OpError("int: invalid literal")
        machine.meter.charge_bytes(result.size())
        stack.append(result)
    elif tag in FLOAT_TAGS:
        try:
            decoded = _expr_to_fp64(v)
            result = int_(int(decoded))
        except (ValueError, OverflowError):
            raise OpError("int: overflow")
        machine.meter.charge_bytes(result.size())
        stack.append(result)
    elif tag == "int":
        machine.meter.charge_bytes(v.size())
        stack.append(v)
    else:
        raise OpError(f"int of {tag}")


def handle_stack_int_with_result(machine, stack, env):
    """Convert value to integer with result wrapping.
    
    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.
    """
    try:
        handle_stack_int(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))
