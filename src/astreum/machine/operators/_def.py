from typing import List

from astreum.machine.environment import Env
from astreum.expression import Expr, NIL, get_expr_tag, link, str_, symbol
from astreum.machine import OpError


def handle_stack_def(machine, stack: List[Expr], env: Env) -> None:
    """Define a new variable in the environment.

    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.

    Returns: None
    """
    if len(stack) < 2:
        raise OpError("stack underflow")

    name = stack.pop()
    value = stack.pop()

    name_tag = get_expr_tag(name)
    if name_tag != "symbol":
        raise OpError(f"def of {name_tag}")

    # Charge: symbol utf8 bytes + serialized value bytes
    cost = name.size() + value.size()
    machine.meter.charge_bytes(cost)

    target = env
    if name.value in target.data:
        raise OpError("def already exists")

    target.put(key=name.value, value=value)


def handle_stack_def_with_result(machine, stack, env):
    """Define a new variable with result wrapping.

    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.

    Returns: None
    """
    try:
        handle_stack_def(machine, stack, env)
        stack.append(link(NIL, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))
