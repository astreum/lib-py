from typing import TYPE_CHECKING, Callable, List

from astreum.expression import Expr, NIL, get_expr_tag
from astreum.machine import OpError

if TYPE_CHECKING:
    from astreum.machine.main import Machine


def _evaluation(machine, expr, stack, env):
    """Lazy import to break circular dependency."""
    from astreum.machine.evaluator import evaluation
    return evaluation(machine, expr, stack, env)


def _step(machine: "Machine", fn: Expr, env, pre_stack: List[Expr]) -> Expr:
    """Evaluate one iteration of a program (quotation or spec).

    The program runs on a copy of ``pre_stack``. Dispatch (eval-style vs
    apply-style) is explicit in the program itself, not inferred from shape.
    """
    cost = sum(e.size() for e in pre_stack)
    machine.meter.charge_bytes(cost + 1)
    result_stack = _evaluation(machine, fn, list(pre_stack), env)
    if not result_stack:
        return NIL
    return result_stack.pop()


def _step_for(fn: Expr) -> Callable:
    """Validate that ``fn`` is a program and return the per-element step.

    Non-program values (scalars, tagged function values, tagged results) are
    rejected loudly — evaluating them as a program would silently corrupt.
    Tagged fns must be apply-wrapped: ``'((fn) apply)``.
    """
    tag = get_expr_tag(fn)
    if tag != "link":
        raise OpError(f"fn of {tag}")
    return _step
