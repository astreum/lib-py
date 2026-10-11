from typing import List

from astreum.expression import Expr, NIL, bytes_, get_expr_tag, get_expr_builtin_tag, link, str_, symbol
from astreum.machine import OpError


def _split_bytes(value, k):
    if k < 0 or k > len(value.value):
        raise OpError(
            f"split index {k} out of bounds for bytes of length {len(value.value)}"
        )
    left = bytes_(value.value[:k])
    right = bytes_(value.value[k:])
    return link(left, right), left.size() + right.size()


def _split_str(value, k):
    chars = list(value.value)
    if k < 0 or k > len(chars):
        raise OpError(
            f"split index {k} out of bounds for str of length {len(chars)}"
        )
    left = str_("".join(chars[:k]))
    right = str_("".join(chars[k:]))
    encoded = left.value.encode("utf-8") + right.value.encode("utf-8")
    return link(left, right), len(encoded)


def _split_link(value, k):
    if k < 0:
        raise OpError(
            f"split index {k} out of bounds for link"
        )
    if k == 0:
        return link(NIL, value), value.size()
    if value is NIL:
        raise OpError(
            f"split index {k} out of bounds for link of length 0"
        )

    prefix_tail = value
    walked = 0
    while walked < k and get_expr_builtin_tag(prefix_tail) == "link" and prefix_tail.head is not None:
        walked += 1
        if walked == k:
            suffix = prefix_tail.tail
            if suffix is None:
                suffix = NIL
            prefix_tail.tail = NIL
            return link(value, suffix), value.size() + suffix.size()
        if prefix_tail.tail is NIL or prefix_tail.tail is None:
            raise OpError(
                f"split index {k} out of bounds for link of length {walked}"
            )
        prefix_tail = prefix_tail.tail

    if walked == k:
        return link(value, prefix_tail), value.size() + prefix_tail.size()

    raise OpError(
        f"split index {k} out of bounds for link of length {walked}"
    )


def handle_stack_split(machine, stack: List[Expr], env) -> None:
    index = stack.pop()
    value = stack.pop()
    value_tag = get_expr_tag(value)
    index_tag = get_expr_tag(index)

    if index_tag != "int":
        raise OpError(
            f"split of {value_tag} at {index_tag}"
        )

    if value_tag == "bytes":
        result, cost = _split_bytes(value, index.value)
    elif value_tag == "str":
        result, cost = _split_str(value, index.value)
    elif value_tag == "link":
        result, cost = _split_link(value, index.value)
    else:
        raise OpError(
            f"split of {value_tag} at int"
        )

    machine.meter.charge_bytes(cost)
    stack.append(result)


def handle_stack_split_with_result(machine, stack, env):
    try:
        handle_stack_split(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))
