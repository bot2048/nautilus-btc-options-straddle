"""
End-to-end scenarios for the straddle, run through NautilusTrader's backtest engine.

Prices are in USDT with a tick of 5, as on Bybit. In most tests the straddle is sold
at call bid 1000 + put bid 1000 = 2000, so the Level 2 triggers sit at
stop-loss >= 2020 (+1%) and take-profit <= 1960 (-2%).
"""

from decimal import Decimal

from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.enums import OptionKind
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.enums import TradingState
from nautilus_trader.model.identifiers import PositionId
from nautilus_trader.model.identifiers import StrategyId
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.position import Position
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.test_kit.stubs.execution import TestExecStubs

from straddle.strategy import ShortStraddle
from straddle.strategy import State
from tests.helpers import find
from tests.helpers import index
from tests.helpers import make_chain
from tests.helpers import quote
from tests.helpers import run

CHAIN = make_chain()
CALL = find(CHAIN, OptionKind.CALL, 74000)
PUT = find(CHAIN, OptionKind.PUT, 74000)

LEVEL_2 = {"stop_loss_pct": Decimal("0.01"), "take_profit_pct": Decimal("0.02")}
EMPTY = {"bid_size": "0.00", "ask_size": "0.00"}  # How an empty book side arrives from Bybit

# Reference price arrives, then both legs quote 1000 / 1005
ENTRY = [
    index(0, "74180.00"),
    quote(CALL, 1, 1000, 1005),
    quote(PUT, 1, 1000, 1005),
]


def orders(engine, side: OrderSide) -> list:
    return [order for order in engine.cache.orders() if order.side == side]


def net_position(engine, instrument) -> Decimal:
    return engine.portfolio.net_position(instrument.id)


def assert_flat_and_never_long(engine) -> None:
    assert not engine.cache.positions_open()
    bought = sum(order.filled_qty.as_decimal() for order in orders(engine, OrderSide.BUY))
    sold = sum(order.filled_qty.as_decimal() for order in orders(engine, OrderSide.SELL))
    assert bought == sold


# -- LEVEL 1 --------------------------------------------------------------------------------------


def test_selects_atm_legs_and_sells_both_at_the_bid():
    engine, strategy = run(ENTRY, options=CHAIN)

    assert strategy._state is State.OPEN
    sells = orders(engine, OrderSide.SELL)
    assert {order.instrument_id for order in sells} == {CALL.id, PUT.id}
    for order in sells:
        assert order.status == OrderStatus.FILLED
        assert order.time_in_force == TimeInForce.IOC
        assert order.price == Price.from_int(1000)
        assert order.quantity == Quantity.from_str("0.01")
    assert net_position(engine, CALL) == Decimal("-0.01")
    assert net_position(engine, PUT) == Decimal("-0.01")
    assert strategy._entry_premium == Decimal(2000)


def test_level_1_never_exits_whatever_the_premium_does():
    data = [
        *ENTRY,
        quote(CALL, 2, 3000, 3005),  # Buyback far above entry
        quote(PUT, 3, 5, 10),
        quote(CALL, 4, 5, 10),  # Buyback far below entry
    ]
    engine, strategy = run(data, options=CHAIN)

    assert strategy._state is State.OPEN
    assert not orders(engine, OrderSide.BUY)
    assert net_position(engine, CALL) == Decimal("-0.01")
    assert net_position(engine, PUT) == Decimal("-0.01")


def test_later_index_prices_do_not_change_the_selected_legs():
    data = [*ENTRY, index(2, "99000.00"), quote(CALL, 3, 1000, 1005)]
    engine, strategy = run(data, options=CHAIN)

    assert strategy._call.id == CALL.id
    assert len(orders(engine, OrderSide.SELL)) == 2


def test_waits_for_a_two_sided_quote_on_both_legs():
    data = [
        index(0, "74180.00"),
        quote(CALL, 1, 1000, 1005),  # Put has not quoted yet
        quote(PUT, 2, 0, 0, **EMPTY),  # Empty put book
        quote(PUT, 3, 1000, 0, ask_size="0.00"),  # Put bid only: could sell, could never value
        quote(CALL, 4, 1000, 1005),
    ]
    engine, strategy = run(data, options=CHAIN)

    assert strategy._state is State.WAITING_FOR_QUOTES
    assert not engine.cache.orders()


def test_waits_until_both_bids_can_take_the_whole_order():
    thin = [
        index(0, "74180.00"),
        quote(CALL, 1, 1000, 1005, bid_size="0.02"),  # 0.02 bid for an order of 0.05
        quote(PUT, 1, 1000, 1005),
    ]
    engine, strategy = run(thin, options=CHAIN, quantity=Decimal("0.05"))
    assert strategy._state is State.WAITING_FOR_QUOTES
    assert not engine.cache.orders()

    engine, strategy = run(
        [*thin, quote(CALL, 2, 1000, 1005)], options=CHAIN, quantity=Decimal("0.05")
    )
    assert strategy._state is State.OPEN
    assert net_position(engine, CALL) == net_position(engine, PUT) == Decimal("-0.05")


def test_selection_follows_the_index_until_the_orders_go_out():
    call_75000 = find(CHAIN, OptionKind.CALL, 75000)
    put_75000 = find(CHAIN, OptionKind.PUT, 75000)
    data = [
        index(0, "74180.00"),  # 74000 is at the money
        quote(CALL, 1, 1000, 1005),  # The 74000 put never quotes
        index(2, "74900.00"),  # Now 75000 is at the money, noticed at the 10s re-check
        quote(PUT, 11, 1000, 1005),  # Too late for the old pair
        quote(call_75000, 12, 800, 805),
        quote(put_75000, 12, 900, 905),
    ]
    engine, strategy = run(data, options=CHAIN)

    assert strategy._state is State.OPEN
    assert {order.instrument_id for order in orders(engine, OrderSide.SELL)} == {
        call_75000.id,
        put_75000.id,
    }
    assert strategy._entry_premium == Decimal(1700)


def test_quotes_cached_before_the_pair_was_selected_are_not_traded_on():
    stale = [
        quote(CALL, 0, 1000, 1005),  # Both in the cache before the selection at 1s
        quote(PUT, 0, 1000, 1005),
        index(1, "74180.00"),
        quote(CALL, 2, 1000, 1005),  # Call is fresh, the put quote is still the old one
    ]
    engine, strategy = run(stale, options=CHAIN)
    assert strategy._state is State.WAITING_FOR_QUOTES
    assert not engine.cache.orders()

    engine, strategy = run([*stale, quote(PUT, 3, 1000, 1005)], options=CHAIN)
    assert strategy._state is State.OPEN


def test_rejects_a_quantity_the_venue_would_round_or_refuse():
    for quantity in ("0.015", "0.001", "0"):
        engine, strategy = run(ENTRY, options=CHAIN, quantity=Decimal(quantity))
        assert strategy._state is State.DONE
        assert not engine.cache.orders()


# -- LEVEL 2: TRIGGERS ----------------------------------------------------------------------------


def test_stop_loss_fires_at_exactly_one_percent_above_entry():
    below = [*ENTRY, quote(CALL, 2, 1005, 1010)]  # Buyback 1010 + 1005 = 2015
    engine, strategy = run(below, options=CHAIN, **LEVEL_2)
    assert strategy._state is State.OPEN
    assert strategy._stop_level == Decimal("2020.00")

    at_level = [*below, quote(PUT, 3, 1005, 1010)]  # Buyback 1010 + 1010 = 2020
    engine, strategy = run(at_level, options=CHAIN, **LEVEL_2)
    assert strategy._state is State.DONE
    assert_flat_and_never_long(engine)


def test_take_profit_fires_at_exactly_two_percent_below_entry():
    above = [*ENTRY, quote(CALL, 2, 980, 985), quote(PUT, 3, 975, 980)]  # Buyback 1965
    engine, strategy = run(above, options=CHAIN, **LEVEL_2)
    assert strategy._state is State.OPEN
    assert strategy._take_level == Decimal("1960.00")

    at_level = [*above, quote(CALL, 4, 975, 980)]  # Buyback 980 + 980 = 1960
    engine, strategy = run(at_level, options=CHAIN, **LEVEL_2)
    assert strategy._state is State.DONE
    assert_flat_and_never_long(engine)


def test_exit_buys_back_both_legs_with_reduce_only_market_orders():
    data = [*ENTRY, quote(CALL, 2, 1040, 1050)]
    engine, _ = run(data, options=CHAIN, **LEVEL_2)

    buys = orders(engine, OrderSide.BUY)
    assert {order.instrument_id for order in buys} == {CALL.id, PUT.id}
    for order in buys:
        assert order.status == OrderStatus.FILLED
        assert order.is_reduce_only
        assert order.quantity == Quantity.from_str("0.01")


def test_entry_premium_uses_fill_prices_not_quoted_prices():
    # Orders take 50ms to arrive and the call bid improves meanwhile, so the call
    # fills at 1100 although its limit price was 1000.
    data = [*ENTRY, quote(CALL, 2, 1100, 1105)]
    _, strategy = run(data, options=CHAIN, latency_ms=50, **LEVEL_2)

    assert strategy._entry_premium == Decimal(2100)
    assert strategy._stop_level == Decimal("2121.00")
    assert strategy._take_level == Decimal("2058.00")


# -- LEVEL 2: DUPLICATES, EMPTY BOOKS, PARTIAL FILLS ----------------------------------------------


def test_repeated_triggers_send_one_exit_only():
    data = [
        *ENTRY,
        quote(CALL, 2, 1040, 1050),  # Stop-loss
        quote(PUT, 2, 1040, 1050),  # Still beyond the stop, same instant
        quote(CALL, 2.001, 1060, 1070),
        quote(PUT, 2.5, 1060, 1070),
        quote(CALL, 3, 900, 905),  # Now beyond the take-profit instead
        quote(PUT, 3, 900, 905),
    ]
    engine, strategy = run(data, options=CHAIN, **LEVEL_2)

    assert strategy._state is State.DONE
    assert len(orders(engine, OrderSide.BUY)) == 2
    assert_flat_and_never_long(engine)


def test_empty_ask_is_not_read_as_a_zero_premium():
    data = [
        *ENTRY,
        quote(CALL, 2, 0, 0, **EMPTY),  # Would make the buyback 0 + 1005, far below take-profit
        quote(PUT, 3, 1000, 0, ask_size="0.00"),
    ]
    engine, strategy = run(data, options=CHAIN, **LEVEL_2)

    assert strategy._state is State.OPEN
    assert not orders(engine, OrderSide.BUY)


def test_one_leg_not_filled_buys_back_the_other():
    # The put bid drops while the orders are in flight: the call fills, the put IOC cancels
    data = [
        *ENTRY,
        quote(PUT, 1.01, 950, 960),
        quote(CALL, 2, 1000, 1005),
        quote(PUT, 2, 950, 960),
    ]
    engine, strategy = run(data, options=CHAIN, latency_ms=50, **LEVEL_2)

    call_sell, put_sell = orders(engine, OrderSide.SELL)
    assert call_sell.status == OrderStatus.FILLED
    assert put_sell.status == OrderStatus.CANCELED
    assert strategy._entry_premium is None  # Never treated as an open straddle
    assert strategy._state is State.DONE
    assert_flat_and_never_long(engine)


def test_partially_filled_entry_buys_back_everything():
    # The call bid shrinks to 0.02 while the orders for 0.05 are in flight
    data = [
        *ENTRY,
        quote(CALL, 1.01, 1000, 1005, bid_size="0.02"),
        quote(PUT, 2, 1000, 1005),
        quote(CALL, 3, 1000, 1005),
    ]
    engine, strategy = run(data, options=CHAIN, quantity=Decimal("0.05"), latency_ms=50, **LEVEL_2)

    call_sell, put_sell = orders(engine, OrderSide.SELL)
    assert call_sell.filled_qty == Quantity.from_str("0.02")
    assert put_sell.filled_qty == Quantity.from_str("0.05")
    assert strategy._state is State.DONE
    assert_flat_and_never_long(engine)


def test_neither_leg_filled_ends_without_a_position():
    data = [
        *ENTRY,
        quote(CALL, 1.01, 950, 960),
        quote(PUT, 1.01, 950, 960),
        quote(CALL, 2, 950, 960),
    ]
    engine, strategy = run(data, options=CHAIN, latency_ms=50, **LEVEL_2)

    assert strategy._state is State.DONE
    assert not engine.cache.positions()
    assert not orders(engine, OrderSide.BUY)


def test_entry_orders_that_never_report_back_are_not_waited_on_forever():
    # Orders take 10s to reach the exchange. After 3s the entry is written off, and when
    # the orders fill after all, the positions are bought back.
    data = [*ENTRY, *[quote(CALL, 2 + step, 1000, 1005) for step in range(15)]]
    engine, strategy = run(data, options=CHAIN, latency_ms=10_000, **LEVEL_2)

    assert strategy._entry_premium is None
    assert len(orders(engine, OrderSide.SELL)) == 2
    assert strategy._state is State.DONE
    assert_flat_and_never_long(engine)


class LateFill(ShortStraddle):
    """
    Delivers a fill for the call entry order after that order was reported canceled, as
    can happen on Bybit, where order status and fills travel on separate streams.
    """

    exec_engine = None
    late_fill_sent = False

    def _finish(self, reason: str) -> None:
        super()._finish(reason)
        if not self.late_fill_sent:
            self.late_fill_sent = True
            call_order = self._entry_orders[0]
            assert call_order.status == OrderStatus.CANCELED
            fill = TestEventStubs.order_filled(
                call_order,
                instrument=CALL,
                last_px=Price.from_int(1000),
                last_qty=Quantity.from_str("0.01"),
                account_id=self.cache.account_for_venue(CALL.id.venue).id,
                ts_event=self.clock.timestamp_ns(),
            )
            self.exec_engine.process(fill)


def test_fill_arriving_after_the_entry_was_written_off_is_bought_back():
    data = [
        *ENTRY,
        quote(CALL, 1.01, 950, 960),  # Both bids drop while the orders are in flight
        quote(PUT, 1.01, 950, 960),
        *[quote(CALL, 2 + step / 10, 950, 960) for step in range(20)],  # Time passes
    ]

    def setup(engine) -> None:
        engine.trader.strategies()[0].exec_engine = engine.kernel.exec_engine

    engine, strategy = run(
        data,
        options=CHAIN,
        strategy_cls=LateFill,
        setup=setup,
        latency_ms=50,
        **LEVEL_2,
    )

    call_sell, put_sell = orders(engine, OrderSide.SELL)
    assert call_sell.status == OrderStatus.FILLED  # The late fill was applied
    assert put_sell.status == OrderStatus.CANCELED
    assert len(orders(engine, OrderSide.BUY)) == 1
    assert strategy._state is State.DONE
    assert_flat_and_never_long(engine)


class PartialCloses(ShortStraddle):
    """
    Stand-in for a thin book: the first close order for each leg buys back only 0.02.
    """

    def __init__(self, config) -> None:
        super().__init__(config)
        self.closes_sent = 0

    def close_position(self, position, *args, **kwargs) -> None:
        self.closes_sent += 1
        if self.closes_sent > 2:
            super().close_position(position, *args, **kwargs)
            return
        order = self.order_factory.market(
            instrument_id=position.instrument_id,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_str("0.02"),
            reduce_only=True,
        )
        self.submit_order(order, position_id=position.id)


def test_partially_filled_exit_buys_back_only_the_remainder():
    data = [
        *ENTRY,
        quote(CALL, 2, 1040, 1050),  # Stop-loss
        *[quote(CALL, 2 + step / 10, 1040, 1050) for step in range(1, 20)],  # Time passes
    ]
    engine, strategy = run(
        data,
        options=CHAIN,
        strategy_cls=PartialCloses,
        quantity=Decimal("0.05"),
        market_exit_interval_ms=100,
        **LEVEL_2,
    )

    assert sorted(str(order.quantity) for order in orders(engine, OrderSide.BUY)) == [
        "0.02",
        "0.02",
        "0.03",
        "0.03",
    ]
    assert strategy._state is State.DONE
    assert_flat_and_never_long(engine)


class StuckCloses(ShortStraddle):
    """
    Stand-in for a venue that will not close anything for a while.
    """

    def __init__(self, config) -> None:
        super().__init__(config)
        self.closes_refused = 0

    def close_position(self, position, *args, **kwargs) -> None:
        if self.clock.timestamp_ns() < self.unstuck_at_ns:
            self.closes_refused += 1
            return
        super().close_position(position, *args, **kwargs)


def test_exit_that_cannot_complete_keeps_retrying_until_flat():
    data = [
        *ENTRY,
        quote(CALL, 2, 1040, 1050),  # Stop-loss
        *[quote(CALL, 2 + step / 10, 1040, 1050) for step in range(1, 60)],  # Time passes
    ]

    def setup(engine) -> None:
        engine.trader.strategies()[0].unstuck_at_ns = data[-1].ts_init - 2_000_000_000

    engine, strategy = run(
        data,
        options=CHAIN,
        strategy_cls=StuckCloses,
        setup=setup,
        market_exit_interval_ms=100,
        market_exit_max_attempts=5,  # Gives up after 0.5s ...
        exit_retry_secs=1,  # ... and starts over 1s later
        **LEVEL_2,
    )

    assert strategy.closes_refused > 10  # Several exit rounds ran while stuck
    assert strategy._state is State.DONE
    assert_flat_and_never_long(engine)


# -- STARTUP --------------------------------------------------------------------------------------


def test_refuses_to_start_on_top_of_an_existing_option_position():
    def setup(engine) -> None:
        # A short call left behind by an earlier run, as startup reconciliation restores it
        order = TestExecStubs.limit_order(
            instrument=CALL,
            order_side=OrderSide.SELL,
            price=Price.from_int(1000),
            quantity=Quantity.from_str("0.01"),
            strategy_id=StrategyId("EXTERNAL"),
        )
        fill = TestEventStubs.order_filled(
            order, instrument=CALL, position_id=PositionId("LEFTOVER")
        )
        engine.cache.add_position(Position(instrument=CALL, fill=fill), OmsType.NETTING)

    engine, strategy = run(ENTRY, options=CHAIN, setup=setup, **LEVEL_2)

    assert strategy._state is State.DONE
    assert not engine.cache.orders()


def test_orders_denied_before_reaching_the_venue_end_cleanly():
    def setup(engine) -> None:
        # The RiskEngine denies every order, synchronously inside `submit_order`
        engine.kernel.risk_engine.set_trading_state(TradingState.HALTED)

    engine, strategy = run(ENTRY, options=CHAIN, setup=setup, **LEVEL_2)

    assert [order.status for order in engine.cache.orders()] == [OrderStatus.DENIED] * 2
    assert strategy._state is State.DONE
    assert not engine.cache.positions()
