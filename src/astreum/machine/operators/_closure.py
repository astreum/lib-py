from typing import TYPE_CHECKING, List

from astreum.expression import Expr, NIL, link, symbol, str_
from astreum.machine import OpError

if TYPE_CHECKING:
    from astreum.machine.main import Machine


def handle_stack_closure(machine: "Machine", stack: List[Expr], env) -> None:
    if not stack:
        raise OpError("stack underflow")
    body = stack.pop()
    if not stack:
        raise OpError("stack underflow")
    params = stack.pop()

    if params._tag != "link":
        raise OpError(f"closure of {params._tag}")

    machine.meter.charge_bytes(params.size() + body.size())

    env_hash = machine.snapshot_env(env)
    env_ref = Expr("link", head_hash=env_hash, tail=symbol("env"))
    body_with_env = link(env_ref, body)
    closure_val = link(link(body_with_env, params), symbol("lex"))
    stack.append(closure_val)


def handle_stack_closure_with_result(machine, stack, env):
    try:
        handle_stack_closure(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))
