from typing import List

from astreum.expression import Expr, NIL, bytes_, FLOAT_TAGS, _expr_to_fp64, get_expr_tag, get_int_from_expr, link, str_, symbol
from astreum.machine import OpError


def _compare_numbers(machine, stack: List[Expr], predicate, verb: str) -> None:
    """Compare two numbers using a predicate.

    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        predicate: Comparison function.
        verb: Description of comparison for error messages.

    Returns: None
    """
    b = stack.pop()
    a = stack.pop()

    a_tag = get_expr_tag(a)
    b_tag = get_expr_tag(b)
    if a_tag == "int" and b_tag == "int":
        result = predicate(get_int_from_expr(a), get_int_from_expr(b))
    elif a_tag in FLOAT_TAGS and b_tag in FLOAT_TAGS:
        # Comparison requires same type, compare decoded fp64 values
        if a_tag != b_tag:
            raise OpError(f"{verb} of {a_tag} and {b_tag}")
        a_decoded = _expr_to_fp64(a)
        b_decoded = _expr_to_fp64(b)
        result = predicate(a_decoded, b_decoded)
    else:
        raise OpError(
            f"{verb} of {a_tag} and {b_tag}"
        )

    result_expr = bytes_(b"\x01" if result else b"\x00")
    machine.meter.charge_bytes(result_expr.size())
    stack.append(result_expr)


def handle_stack_less_than(machine, stack: List[Expr], env) -> None:
    """Compare if a < b.

    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.

    Returns: None
    """
    _compare_numbers(machine, stack, lambda a, b: a < b, "less than")


def handle_stack_greater_than(machine, stack: List[Expr], env) -> None:
    """Compare if a > b.

    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.

    Returns: None
    """
    _compare_numbers(machine, stack, lambda a, b: a > b, "greater than")


def handle_stack_less_than_or_equal(machine, stack: List[Expr], env) -> None:
    """Compare if a <= b.

    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.

    Returns: None
    """
    _compare_numbers(machine, stack, lambda a, b: a <= b, "less than or equal")


def handle_stack_greater_than_or_equal(machine, stack: List[Expr], env) -> None:
    """Compare if a >= b.

    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.

    Returns: None
    """
    _compare_numbers(machine, stack, lambda a, b: a >= b, "greater than or equal")


def handle_stack_less_than_with_result(machine, stack, env):
    """Compare if a < b with result wrapping.

    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.

    Returns: None
    """
    try:
        handle_stack_less_than(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))


def handle_stack_greater_than_with_result(machine, stack, env):
    """Compare if a > b with result wrapping.

    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.

    Returns: None
    """
    try:
        handle_stack_greater_than(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))


def handle_stack_less_than_or_equal_with_result(machine, stack, env):
    """Compare if a <= b with result wrapping.

    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.

    Returns: None
    """
    try:
        handle_stack_less_than_or_equal(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))


def handle_stack_greater_than_or_equal_with_result(machine, stack, env):
    """Compare if a >= b with result wrapping.

    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.

    Returns: None
    """
    try:
        handle_stack_greater_than_or_equal(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))
