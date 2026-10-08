from typing import TYPE_CHECKING, List

from astreum.machine.environment import Env
from astreum.expression import Expr, NIL, link, str_, symbol
from astreum.machine import OpError
from astreum.machine.operators._if import is_truthy

if TYPE_CHECKING:
    from astreum.machine.main import Machine


def _evaluation(machine, expr, stack, env):
    """Lazy import to break circular dependency.

    Args:
        machine: The machine instance.
        expr: The expression to evaluate.
        stack: The evaluation stack.
        env: The environment.

    Returns: None
    """
    from astreum.machine.evaluator import evaluation

    return evaluation(machine, expr, stack, env)


def handle_stack_rec(
    machine: "Machine", stack: List[Expr], env: Env
) -> List[Expr]:
    """Handle recursion operator.
    
    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.
    
    Returns:
        The updated stack.
    """
    if len(stack) < 4:
        raise OpError("stack underflow")

    rec2 = stack.pop()
    rec1 = stack.pop()
    then_branch = stack.pop()
    pred = stack.pop()

    rec2_count = 0
    while True:
        machine.meter.charge_bytes(pred.size())
        stack = _evaluation(machine, pred, stack, env)

        condition = stack.pop()
        machine.meter.charge_bytes(condition.size())

        if is_truthy(condition):
            machine.meter.charge_bytes(then_branch.size())
            stack = _evaluation(machine, then_branch, stack, env)
            break

        machine.meter.charge_bytes(rec1.size())
        stack = _evaluation(machine, rec1, stack, env)
        rec2_count += 1

    for _ in range(rec2_count):
        machine.meter.charge_bytes(rec2.size())
        stack = _evaluation(machine, rec2, stack, env)

    return stack


def handle_stack_rec_with_result(machine, stack, env):
    """Handle recursion operator with result wrapping.
    
    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.
    
    Returns:
        The updated stack with result wrapped in ok/err tag.
    """
    try:
        stack = handle_stack_rec(machine, stack, env)
        top = stack.pop()
        stack.append(link(top, symbol("ok")))
        return stack
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
        return stack
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))
        return stack
