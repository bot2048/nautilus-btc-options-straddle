"""
ATM straddle selection.

Kept free of any live-trading machinery so it can be unit tested with plain
instrument objects.
"""

from collections import defaultdict
from decimal import Decimal

from nautilus_trader.model.enums import OptionKind
from nautilus_trader.model.instruments import CryptoOption


def select_atm_straddle(
    options: list[CryptoOption],
    reference_price: Decimal,
    now_ns: int,
    min_ns_to_expiry: int,
) -> tuple[CryptoOption, CryptoOption] | None:
    """
    Return the (call, put) pair for an ATM straddle, or ``None`` if there is none.

    The expiry is the nearest one that is at least `min_ns_to_expiry` away. Within
    that expiry the strike is the one closest to `reference_price`, considering only
    strikes which list both a call and a put. An exact tie between two strikes goes
    to the lower one.
    """
    # expiry -> strike -> {kind: instrument}
    chains: dict[int, dict[Decimal, dict[OptionKind, CryptoOption]]] = defaultdict(
        lambda: defaultdict(dict),
    )
    for option in options:
        if option.expiration_ns - now_ns < min_ns_to_expiry:
            continue  # Expired (Bybit keeps listing these for a while) or too close to expiry
        chains[option.expiration_ns][option.strike_price.as_decimal()][option.option_kind] = option

    for expiry in sorted(chains):
        strikes = sorted(
            strike
            for strike, legs in chains[expiry].items()
            if OptionKind.CALL in legs and OptionKind.PUT in legs
        )
        if not strikes:
            continue  # No complete call/put pair in this expiry, try the next one
        atm_strike = min(strikes, key=lambda strike: abs(strike - reference_price))
        legs = chains[expiry][atm_strike]
        return legs[OptionKind.CALL], legs[OptionKind.PUT]

    return None
