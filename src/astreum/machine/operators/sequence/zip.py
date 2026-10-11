from typing import List

from astreum.expression import Expr, NIL, bytes_, get_expr_tag, get_expr_builtin_tag, link, str_, symbol
from astreum.machine import OpError


def _seq_length(tag, value):
    if tag == "bytes":
        return len(value.value)
    if tag == "str":
        return len(list(value.value))
    if tag == "link":
        n = 0
        current = value
        while get_expr_builtin_tag(current) == "link" and current.head is not None:
            n += 1
            if current.tail is NIL or current.tail is None:
                break
            current = current.tail
        return n
    return 0


def handle_stack_zip(machine, stack: List[Expr], env) -> None:
    b = stack.pop()
    a = stack.pop()

    a_tag, b_tag = get_expr_tag(a), get_expr_tag(b)
    if a_tag not in ("bytes", "str", "link") or b_tag not in ("bytes", "str", "link"):
        raise OpError(f"zip of {a_tag} and {b_tag}")

    len_a = _seq_length(a_tag, a)
    len_b = _seq_length(b_tag, b)
    pairs = len_a if len_a < len_b else len_b
    machine.meter.charge_bytes(pairs)

    def elem_at(tag, value, i):
        if tag == "bytes":
            return bytes_(value.value[i:i + 1])
        if tag == "str":
            return str_(list(value.value)[i])
        if tag == "link":
            current = value
            walked = 0
            while get_expr_builtin_tag(current) == "link" and current.head is not None:
                if walked == i:
                    return current.head
                walked += 1
                current = current.tail
            return NIL
        return NIL

    elems = []
    for i in range(pairs):
        ai = elem_at(a_tag, a, i)
        bi = elem_at(b_tag, b, i)
        pair = link(ai, bi)
        elems.append((pair, ai.size() + bi.size()))

    cost = sum(c for _, c in elems) + pairs
    machine.meter.charge_bytes(cost)
    out = NIL
    for elem, _ in reversed(elems):
        out = link(elem, out)
    stack.append(out)


def handle_stack_zip_with_result(machine, stack, env):
    try:
        handle_stack_zip(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))
