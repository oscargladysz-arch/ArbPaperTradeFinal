"""Integer-tick money math. Rule 5: no float on any price, size, fee, or P&L path.

Units
-----
- 1 dollar = 10_000 ticks, so 1 cent = 100 ticks and 0.01 cent (one hundredth of a cent) = 1 tick.
- Contract prices live in [0, 10_000] ticks. Kalshi's sub-penny prices (yes_price_dollars
  such as "0.4450") are exact in this unit. Polymarket prices such as "0.445" are exact too.
- Anything that cannot be represented exactly raises instead of rounding silently.

Worked example: yes_price 0.44 dollars = 4_400 ticks. Its complement, the NO price the maker
paid when the taker bought YES, is 10_000 - 4_400 = 5_600 ticks = 0.56 dollars.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Final

TICKS_PER_DOLLAR: Final[int] = 10_000
TICKS_PER_CENT: Final[int] = 100
MAX_PRICE_TICKS: Final[int] = TICKS_PER_DOLLAR

_DOLLAR_QUANTUM: Final[Decimal] = Decimal(1) / Decimal(TICKS_PER_DOLLAR)


class MoneyError(ValueError):
    """Raised when a value is not exactly representable or is out of range."""


def ticks_from_decimal_dollars(value: Decimal | str | int) -> int:
    """Convert a Decimal dollar amount to integer ticks, exactly.

    >>> ticks_from_decimal_dollars("0.44")
    4400
    >>> ticks_from_decimal_dollars(Decimal("0.4450"))
    4450
    """
    try:
        d = Decimal(value) if not isinstance(value, Decimal) else value
    except InvalidOperation as exc:
        raise MoneyError(f"not a decimal: {value!r}") from exc
    if not d.is_finite():
        raise MoneyError(f"non-finite amount: {value!r}")
    scaled = d * TICKS_PER_DOLLAR
    if scaled != scaled.to_integral_value():
        raise MoneyError(f"{value!r} is not an exact multiple of 1/{TICKS_PER_DOLLAR} dollar")
    return int(scaled)


def ticks_from_cents(cents: int) -> int:
    """Whole cents to ticks. 44 cents -> 4400 ticks."""
    if not isinstance(cents, int) or isinstance(cents, bool):
        raise MoneyError(f"cents must be int, got {type(cents).__name__}")
    return cents * TICKS_PER_CENT


def to_decimal_dollars(ticks: int) -> Decimal:
    """Ticks to an exact Decimal dollar amount. 4400 -> Decimal('0.4400')."""
    _check_int(ticks)
    return (Decimal(ticks) * _DOLLAR_QUANTUM).quantize(_DOLLAR_QUANTUM)


def price_ticks(value: Decimal | str | int) -> int:
    """Parse a contract price in dollars and validate it lies in [0, 1]."""
    t = ticks_from_decimal_dollars(value)
    check_price(t)
    return t


def check_price(ticks: int) -> None:
    _check_int(ticks)
    if ticks < 0 or ticks > MAX_PRICE_TICKS:
        raise MoneyError(f"price {ticks} ticks outside [0, {MAX_PRICE_TICKS}]")


def complement(price: int) -> int:
    """The other side's price: NO price = 1 dollar - YES price. 4400 -> 5600."""
    check_price(price)
    return MAX_PRICE_TICKS - price


def round_down_to_tick(price: int, tick_size: int) -> int:
    """Floor a price to a multiple of the market tick size (in ticks).

    Example: FV - e = 5_250 ticks (0.525) on a 1-cent market (tick_size = 100) -> 5_200.
    """
    check_price(price)
    if tick_size <= 0:
        raise MoneyError(f"tick_size must be positive, got {tick_size}")
    return (price // tick_size) * tick_size


def round_up_to_tick(price: int, tick_size: int) -> int:
    check_price(price)
    if tick_size <= 0:
        raise MoneyError(f"tick_size must be positive, got {tick_size}")
    return -((-price) // tick_size) * tick_size


def notional_ticks(price: int, contracts: int) -> int:
    """Total cost in ticks of `contracts` at `price`. 50 contracts * 4400 = 220_000 ticks = $22."""
    check_price(price)
    if contracts < 0:
        raise MoneyError(f"contracts must be non-negative, got {contracts}")
    return price * contracts


def decimal_floor(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_FLOOR)


def _check_int(value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise MoneyError(f"expected int ticks, got {type(value).__name__}")
