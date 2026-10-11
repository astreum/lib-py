from typing import List

from astreum.expression import Expr, NIL, get_expr_tag, get_expr_builtin_tag, link, str_, symbol
from astreum.machine import OpError


def handle_stack_init(machine, stack: List[Expr], env) -> None:
    if len(stack) < 2:
        raise OpError("stack underflow")
    tag_expr = stack.pop()
    value_expr = stack.pop()

    if get_expr_builtin_tag(tag_expr) != "symbol":
        raise OpError(f"init requires a symbol tag, got {get_expr_builtin_tag(tag_expr)}")

    tag = tag_expr.value

    if get_expr_tag(value_expr) == tag:
        stack.append(value_expr)
    else:
        stack.append(Expr("link", head=value_expr, tail=symbol(tag)))


def handle_stack_init_with_result(machine, stack, env):
    try:
        handle_stack_init(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))
