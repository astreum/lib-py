import time

from astreum.expression import int_


def handle_stack_time(machine, stack, env):
    """Push current Unix timestamp onto stack.
    
    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.
    """
    secs = int(time.time())
    val = int_(secs)
    machine.meter.charge_bytes(val.size())
    stack.append(val)


def handle_stack_clock(machine, stack, env):
    """Push high-resolution clock value onto stack.
    
    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.
    """
    ns = time.perf_counter_ns()
    val = int_(ns)
    machine.meter.charge_bytes(val.size())
    stack.append(val)
