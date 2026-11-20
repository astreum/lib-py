from __future__ import annotations


def calculate_discounted_amount(
    *,
    payment_amount: int,
    payment_interval_blocks: int,
    payment_count: int,
    rate_numerator: int,
    rate_denominator: int,
) -> int | None:
    """Return floor(PV) for fixed payments discounted by a per-block rational rate."""
    if payment_amount <= 0:
        return None
    if payment_interval_blocks <= 0 or payment_count <= 0:
        return None
    if rate_denominator <= 0:
        return None

    factor_numerator = rate_denominator + rate_numerator
    if factor_numerator <= 0:
        return None

    if rate_numerator == 0:
        return payment_amount * payment_count

    period_numerator = pow(factor_numerator, payment_interval_blocks)
    period_denominator = pow(rate_denominator, payment_interval_blocks)
    numerator = (
        payment_amount
        * period_denominator
        * (pow(period_numerator, payment_count) - pow(period_denominator, payment_count))
    )
    denominator = (
        pow(period_numerator, payment_count)
        * (period_numerator - period_denominator)
    )
    if denominator == 0:
        return None
    if denominator < 0:
        numerator = -numerator
        denominator = -denominator
    if numerator <= 0:
        return None
    discounted_amount = numerator // denominator
    if discounted_amount < 1:
        return None
    return discounted_amount


