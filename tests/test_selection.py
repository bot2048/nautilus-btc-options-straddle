from decimal import Decimal

from nautilus_trader.model.enums import OptionKind

from straddle.selection import select_atm_straddle
from tests.helpers import EXPIRY
from tests.helpers import HOUR
from tests.helpers import T0
from tests.helpers import make_chain
from tests.helpers import make_option

CALL, PUT = OptionKind.CALL, OptionKind.PUT
MIN_12H = 12 * HOUR


def strikes_of(legs) -> tuple[int, int]:
    call, put = legs
    assert call.option_kind == CALL
    assert put.option_kind == PUT
    assert call.expiration_ns == put.expiration_ns
    return int(call.strike_price), int(put.strike_price)


def test_picks_strike_nearest_to_reference():
    legs = select_atm_straddle(make_chain(), Decimal("74180.5"), T0, MIN_12H)
    assert strikes_of(legs) == (74000, 74000)

    legs = select_atm_straddle(make_chain(), Decimal(74600), T0, MIN_12H)
    assert strikes_of(legs) == (75000, 75000)


def test_exact_tie_goes_to_lower_strike():
    legs = select_atm_straddle(make_chain(), Decimal(74500), T0, MIN_12H)
    assert strikes_of(legs) == (74000, 74000)


def test_reference_outside_listed_strikes_clamps_to_edge():
    assert strikes_of(select_atm_straddle(make_chain(), Decimal(10), T0, MIN_12H)) == (73000, 73000)
    assert strikes_of(select_atm_straddle(make_chain(), Decimal(999999), T0, MIN_12H)) == (
        75000,
        75000,
    )


def test_skips_expired_and_too_near_expiries():
    expired = T0 - HOUR
    too_near = T0 + 3 * HOUR
    options = [
        make_option(CALL, 74000, expired, "09NOV26"),
        make_option(PUT, 74000, expired, "09NOV26"),
        make_option(CALL, 74000, too_near, "10NOV26"),
        make_option(PUT, 74000, too_near, "10NOV26"),
        make_option(CALL, 74000, EXPIRY),
        make_option(PUT, 74000, EXPIRY),
    ]
    call, put = select_atm_straddle(options, Decimal(74000), T0, MIN_12H)
    assert call.expiration_ns == put.expiration_ns == EXPIRY


def test_picks_nearest_eligible_expiry():
    later = EXPIRY + 24 * HOUR
    options = [
        make_option(CALL, 74000, later, "12NOV26"),
        make_option(PUT, 74000, later, "12NOV26"),
        make_option(CALL, 74000, EXPIRY),
        make_option(PUT, 74000, EXPIRY),
    ]
    call, _ = select_atm_straddle(options, Decimal(74000), T0, MIN_12H)
    assert call.expiration_ns == EXPIRY


def test_ignores_strikes_without_both_legs():
    options = [
        make_option(CALL, 74000),  # No put at the closest strike
        make_option(CALL, 75000),
        make_option(PUT, 75000),
    ]
    legs = select_atm_straddle(options, Decimal(74000), T0, MIN_12H)
    assert strikes_of(legs) == (75000, 75000)


def test_falls_through_to_next_expiry_when_nearest_has_no_pair():
    later = EXPIRY + 24 * HOUR
    options = [
        make_option(CALL, 74000, EXPIRY),
        make_option(CALL, 74000, later, "12NOV26"),
        make_option(PUT, 74000, later, "12NOV26"),
    ]
    call, put = select_atm_straddle(options, Decimal(74000), T0, MIN_12H)
    assert call.expiration_ns == put.expiration_ns == later


def test_returns_none_when_nothing_is_tradable():
    assert select_atm_straddle([], Decimal(74000), T0, MIN_12H) is None
    assert select_atm_straddle([make_option(CALL, 74000)], Decimal(74000), T0, MIN_12H) is None
    assert select_atm_straddle(make_chain(), Decimal(74000), EXPIRY, MIN_12H) is None
