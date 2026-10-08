from typing import List

from astreum.machine.environment import Env
from astreum.expression import Expr, NIL, link, FLOAT_TAGS, is_builtin_composite
from astreum.machine import OpError
from astreum.machine.operators.main import OPERATOR_LIST, apply_operator


def evaluation(machine, expr: Expr, stack: List[Expr] = [], env: Env = Env()) -> List[Expr]:
    """Evaluate an expression and manipulate the stack.

    Args:
        machine: The machine instance managing execution state.
        expr: The expression to evaluate.
        stack: The current evaluation stack.
        env: The environment for variable bindings.

    Returns:
        The updated stack after evaluation.
    """

    if expr.base == "symbol":
        if machine.mode == "deterministic":
            if expr.value in OPERATOR_LIST:
                try:
                    stack = apply_operator(machine, expr, stack, env)
                except (OpError, IndexError):
                    machine.meter.charge_bytes(1)
                    stack.append(NIL)
            else:
                bound = env.get(expr.value)
                if bound is None:
                    machine.meter.charge_bytes(expr.size() + 1)
                    stack.append(link(None, None))
                else:
                    machine.meter.charge_bytes(expr.size() + bound.size())
                    stack.append(bound)
        else:
            bound = env.get(expr.value)
            if bound is not None:
                if expr.value in OPERATOR_LIST:
                    machine.meter.charge_bytes(bound.size())
                    try:
                        stack = evaluation(machine, bound, stack, env)
                    except (OpError, IndexError):
                        machine.meter.charge_bytes(1)
                        stack.append(NIL)
                else:
                    machine.meter.charge_bytes(expr.size() + bound.size())
                    stack.append(bound)
            elif expr.value in OPERATOR_LIST:
                try:
                    stack = apply_operator(machine, expr, stack, env)
                except (OpError, IndexError):
                    machine.meter.charge_bytes(1)
                    stack.append(NIL)
            else:
                machine.meter.charge_bytes(expr.size() + 1)
                stack.append(link(None, None))
        return stack

    if expr.base == "bytes" or is_builtin_composite(expr):
        machine.meter.charge_bytes(expr.size())
        stack.append(expr)
        return stack

    if expr.base == "link":
        if expr.head is not None and expr.head.base == "symbol" and expr.head.value == "'":
            if expr.tail is None or expr.tail is NIL:
                machine.meter.charge_bytes(1)
                stack.append(NIL)
            else:
                machine.meter.charge_bytes(expr.tail.head.size())
                stack.append(expr.tail.head)
            return stack
        if expr.head is not None:
            stack = evaluation(machine, expr.head, stack, env)
        if expr.tail is not None:
            stack = evaluation(machine, expr.tail, stack, env)
        return stack
