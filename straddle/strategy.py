"""
BTC ATM short straddle.

Level 1 (naked):   sell one ATM call and one ATM put, then only observe.
Level 2 (managed): the same entry, plus a stop-loss and a take-profit on the
                   combined premium needed to buy both legs back (ask side).

The strategy is a small state machine:

    WAITING_FOR_REFERENCE -> WAITING_FOR_QUOTES -> ENTERING -> OPEN -> EXITING -> DONE

Every transition is driven by an event NautilusTrader delivers to the strategy
(an index price, a quote, an order event), so nothing here blocks or polls.

Log lines that start with ">>" are plain-English summaries added for readability.
"""

from datetime import timedelta
from decimal import Decimal
from enum import Enum

from nautilus_trader.common.events import TimeEvent
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import IndexPriceUpdate
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OptionKind
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.events import OrderCanceled
from nautilus_trader.model.events import OrderDenied
from nautilus_trader.model.events import OrderEvent
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.events import OrderRejected
from nautilus_trader.model.events import PositionClosed
from nautilus_trader.model.events import PositionOpened
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import CryptoOption
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.orders import Order
from nautilus_trader.trading.strategy import Strategy

from straddle.selection import select_atm_straddle

NANOS_PER_HOUR = 3_600_000_000_000  # nanoseconds in one hour
STATUS_TIMER = "STRADDLE-STATUS"  # repeating status-line timer
EXIT_RETRY_TIMER = "STRADDLE-EXIT-RETRY"  # one-shot exit retry alert
ENTRY_TIMEOUT_TIMER = "STRADDLE-ENTRY-TIMEOUT"  # one-shot entry deadline alert


# Where the strategy is in its life; the value is a plain description
class State(Enum):
    WAITING_FOR_REFERENCE = "waiting for the first BTC index price"
    WAITING_FOR_QUOTES = "legs selected, waiting for a two-sided quote on both"
    ENTERING = "both sell orders sent, waiting for them to finish"
    OPEN = "short straddle is on"
    EXITING = "buying both legs back"
    DONE = "finished"


# Settings for the strategy; frozen, so they never change while running
class ShortStraddleConfig(StrategyConfig, frozen=True):
    """
    Configuration for ``ShortStraddle``.

    Parameters
    ----------
    reference_instrument_id : InstrumentId
        Instrument whose index price is the BTC reference price for picking the
        ATM strike (the BTCUSDT perpetual).
    underlying : str
        Underlying of the options to trade.
    settlement : str
        Settlement currency of the options to trade.
    quantity : Decimal
        Contracts to sell on each leg (BTC). Both legs always use the same quantity.
    min_hours_to_expiry : float
        Trade the nearest expiry that is at least this far away.
    stop_loss_pct : Decimal, optional
        Exit when the buyback premium rises this fraction above the entry premium
        (0.01 = 1%). ``None`` disables the stop-loss.
    take_profit_pct : Decimal, optional
        Exit when the buyback premium falls this fraction below the entry premium
        (0.02 = 2%). ``None`` disables the take-profit.
    status_interval_secs : int
        How often to log a one-line status of the straddle.
    entry_timeout_secs : int
        How long to wait for both entry orders to report a final state.
    exit_retry_secs : int
        Pause before trying again when an exit did not get the position flat.

    """

    reference_instrument_id: InstrumentId  # where the BTC index comes from
    underlying: str = "BTC"  # coin the options are on
    settlement: str = "USDT"  # coin the options settle in
    quantity: Decimal = Decimal("0.01")  # BTC per leg
    min_hours_to_expiry: float = 12.0  # skip expiries closer than this
    stop_loss_pct: Decimal | None = None  # None = no stop-loss (Level 1)
    take_profit_pct: Decimal | None = None  # None = no take-profit (Level 1)
    status_interval_secs: int = 10  # status line every N seconds
    entry_timeout_secs: int = 3  # give entry orders N seconds
    exit_retry_secs: int = 5  # wait N seconds before retrying exit


# The strategy itself: reacts to events, keeps one state at a time
class ShortStraddle(Strategy):
    def __init__(self, config: ShortStraddleConfig) -> None:
        super().__init__(config)
        self._state = State.WAITING_FOR_REFERENCE  # current state
        self._reference_price: Decimal | None = None  # latest BTC index
        self._call: CryptoOption | None = None  # chosen call leg
        self._put: CryptoOption | None = None  # chosen put leg
        self._quantity: Quantity | None = None  # Order size per leg, same for both
        self._selected_at_ns = 0  # when the pair was chosen
        self._entry_orders: list[Order] = []  # the two sell orders
        self._entry_premium: Decimal | None = None  # call fill price + put fill price
        self._stop_level: Decimal | None = None  # buyback at/above = stop-loss
        self._take_level: Decimal | None = None  # buyback at/below = take-profit

    # -- LIFECYCLE --------------------------------------------------------------------------------

    # Runs once at startup
    def on_start(self) -> None:
        reference_id = self.config.reference_instrument_id
        # Need the BTC perpetual loaded to read the index
        if self.cache.instrument(reference_id) is None:
            self._finish(f"reference instrument {reference_id} was not loaded")
            return

        # Positions found here were opened by an earlier run and restored by the startup
        # reconciliation. Selling a second straddle on top of them would double the risk.
        leftovers = [
            position.instrument_id
            for position in self.cache.positions_open(venue=reference_id.venue)
            if isinstance(self.cache.instrument(position.instrument_id), CryptoOption)
        ]
        if leftovers:
            self._finish(f"account already holds option positions {leftovers}, close them first")
            return

        self.log.info(">> Strategy started. Waiting for the first BTC index price.")
        self.subscribe_index_prices(reference_id)  # start receiving BTC index
        # Repeating timer for the status line
        self.clock.set_timer(
            name=STATUS_TIMER,
            interval=timedelta(seconds=self.config.status_interval_secs),
            callback=self._on_status_timer,
        )

    # Runs at shutdown
    def on_stop(self) -> None:
        # With `manage_stop=True` NautilusTrader runs a market exit before calling this
        open_positions = self.cache.positions_open(strategy_id=self.id)
        if open_positions:
            self.log.warning(
                f"Stopping with {len(open_positions)} option position(s) still open on the "
                f"venue and no longer monitored: {[str(p) for p in open_positions]}",
            )

    # -- MARKET DATA ------------------------------------------------------------------------------

    # New BTC index price arrived
    def on_index_price(self, index_price: IndexPriceUpdate) -> None:
        self._reference_price = index_price.value.as_decimal()  # remember latest price
        if self._state is State.WAITING_FOR_REFERENCE:
            self._select_legs()  # first price: pick strike

    # New bid/ask quote arrived
    def on_quote_tick(self, tick: QuoteTick) -> None:
        if self._state is State.WAITING_FOR_QUOTES:
            self._try_enter()  # maybe sell now
        elif self._state is State.OPEN:
            self._check_exit()  # maybe stop-loss/take-profit

    # -- ENTRY ------------------------------------------------------------------------------------

    # Pick ATM call+put and subscribe to their quotes
    def _select_legs(self) -> None:
        # All BTC options settled in USDT
        options = [
            instrument
            for instrument in self.cache.instruments(
                venue=self.config.reference_instrument_id.venue
            )
            if isinstance(instrument, CryptoOption)
            and instrument.underlying.code == self.config.underlying
            and instrument.settlement_currency.code == self.config.settlement
        ]
        # Ask the selection function for the ATM pair
        legs = select_atm_straddle(
            options=options,
            reference_price=self._reference_price,
            now_ns=self.clock.timestamp_ns(),
            min_ns_to_expiry=int(self.config.min_hours_to_expiry * NANOS_PER_HOUR),
        )
        if legs is None:
            self._finish(f"no call/put pair found among {len(options)} options")
            return
        if legs == (self._call, self._put):
            return  # The index moved but the same strike is still the closest

        # Quantity must fit the lot size on both legs
        quantity = self.config.quantity
        for leg in legs:
            step = leg.size_increment.as_decimal()
            minimum = leg.min_quantity.as_decimal() if leg.min_quantity else step
            if quantity < minimum or quantity % step != 0:
                # `make_qty` would silently round, and then a different size gets traded
                self._finish(
                    f"quantity {quantity} must be a multiple of {step}, at least {minimum}"
                )
                return

        # Strike changed: drop old quotes
        if self._call is not None:
            self.unsubscribe_quote_ticks(self._call.id)
            self.unsubscribe_quote_ticks(self._put.id)
        self._call, self._put = legs
        self._quantity = self._call.make_qty(quantity)  # size as a Quantity object
        self._selected_at_ns = self.clock.timestamp_ns()  # quotes older than this are stale
        self.log.info(
            f"ATM selection: reference={self._reference_price} strike={self._call.strike_price} "
            f"expiry={self._call.expiration_utc} call={self._call.id} put={self._put.id}",
        )
        self.log.info(
            f">> BTC index is {self._reference_price}, so the closest strike is "
            f"{self._call.strike_price}. Waiting for live bid/ask on its call and put.",
        )
        self._state = State.WAITING_FOR_QUOTES
        self.subscribe_quote_ticks(self._call.id)  # watch the call
        self.subscribe_quote_ticks(self._put.id)  # watch the put

    # Sell both legs if the quotes look tradable
    def _try_enter(self) -> None:
        call_quote = self.cache.quote_tick(self._call.id)  # latest call quote
        put_quote = self.cache.quote_tick(self._put.id)  # latest put quote
        for quote in (call_quote, put_quote):
            # The cache may still hold a quote from before this pair was (re)selected
            if not _can_sell(quote, self._quantity) or quote.ts_init < self._selected_at_ns:
                return  # not ready, wait for next quote

        # IOC limit at the bid: sell at the quoted price or better, never rest in the book.
        # Both orders exist before either is submitted, because a denied order comes back
        # as an event from inside `submit_order` and `on_order_event` looks at both.
        self._entry_orders = [
            self.order_factory.limit(
                instrument_id=instrument.id,
                order_side=OrderSide.SELL,
                quantity=self._quantity,
                price=quote.bid_price,
                time_in_force=TimeInForce.IOC,
            )
            for instrument, quote in ((self._call, call_quote), (self._put, put_quote))
        ]
        self._state = State.ENTERING
        self.log.info(
            f">> Selling {self._quantity} BTC of call at {call_quote.bid_price} and put at "
            f"{put_quote.bid_price} (limit IOC orders at the current best bids).",
        )
        for order in self._entry_orders:
            self.submit_order(order)  # send to the exchange
        # An IOC order is finished at the venue within milliseconds, but the message saying
        # "canceled" can be late or missing (seen on Bybit demo, where it took 45 seconds).
        # Fills arrive on their own stream, so after a few seconds the fills are the answer.
        self.clock.set_time_alert(
            name=ENTRY_TIMEOUT_TIMER,
            alert_time=self.clock.utc_now() + timedelta(seconds=self.config.entry_timeout_secs),
            callback=self._on_entry_timeout,
        )

    # Any order update: check if entry is finished
    def on_order_event(self, event: OrderEvent) -> None:
        if self._state is State.ENTERING and all(order.is_closed for order in self._entry_orders):
            self._conclude_entry()

    # Entry deadline passed: decide from fills
    def _on_entry_timeout(self, event: TimeEvent) -> None:
        if self._state is State.ENTERING:
            self._conclude_entry()

    # Look at both entry orders and decide what next
    def _conclude_entry(self) -> None:
        call_order, put_order = self._entry_orders
        summary = (
            f"call {call_order.status.name} {call_order.filled_qty}/{call_order.quantity}, "
            f"put {put_order.status.name} {put_order.filled_qty}/{put_order.quantity}"
        )

        # Both legs fully sold: straddle is on
        if call_order.status == OrderStatus.FILLED and put_order.status == OrderStatus.FILLED:
            self._entry_premium = _avg_fill_price(call_order) + _avg_fill_price(put_order)
            # Exit levels from the entry premium (Level 2 only)
            if self.config.stop_loss_pct is not None:
                self._stop_level = self._entry_premium * (1 + self.config.stop_loss_pct)
            if self.config.take_profit_pct is not None:
                self._take_level = self._entry_premium * (1 - self.config.take_profit_pct)
            self._state = State.OPEN
            self.log.info(
                f"Straddle open ({summary}): entry premium={self._entry_premium} "
                f"stop-loss at buyback >= {self._stop_level or 'off'}, "
                f"take-profit at buyback <= {self._take_level or 'off'}",
            )
            if self._stop_level is None and self._take_level is None:
                self.log.info(
                    f">> Straddle is on: collected {self._entry_premium} per BTC. "
                    "Level 1 has no automatic exit; press Ctrl-C to buy both back.",
                )
            else:
                self.log.info(
                    f">> Straddle is on: collected {self._entry_premium} per BTC. Buying both "
                    f"back costs the call ask plus the put ask. Stop-loss if that reaches "
                    f"{self._stop_level}, take-profit if it falls to {self._take_level}.",
                )
        elif call_order.filled_qty > 0 or put_order.filled_qty > 0:
            # The legs are unbalanced, which is a directional position this strategy must
            # not hold: buy back whatever was sold.
            self._exit(f"entry incomplete ({summary})")
        else:
            self._finish(f"entry orders did not fill ({summary})")  # nothing sold, no risk

    # -- EXIT -------------------------------------------------------------------------------------

    # Cost now to buy both legs back
    def _buyback_premium(self) -> Decimal | None:
        """
        Return call ask + put ask, or ``None`` while either leg has no ask.
        """
        call_quote = self.cache.quote_tick(self._call.id)
        put_quote = self.cache.quote_tick(self._put.id)
        if not (_has_ask(call_quote) and _has_ask(put_quote)):
            return None  # An empty ask side is published as price 0, which is not a price
        return call_quote.ask_price.as_decimal() + put_quote.ask_price.as_decimal()

    # Compare buyback cost with the two levels
    def _check_exit(self) -> None:
        buyback = self._buyback_premium()
        if buyback is None:
            return  # cannot value, skip

        if self._stop_level is not None and buyback >= self._stop_level:
            self._exit(f"stop-loss: buyback {buyback} >= {self._stop_level}")
        elif self._take_level is not None and buyback <= self._take_level:
            self._exit(f"take-profit: buyback {buyback} <= {self._take_level}")

    # Start closing everything (once)
    def _exit(self, reason: str) -> None:
        # Leaving OPEN is what makes an exit fire once: later quotes no longer reach
        # `_check_exit`.
        self._state = State.EXITING
        self.log.warning(f"Exiting: {reason}")
        self.log.info(">> Buying both options back now with reduce-only market orders.")
        # Cancels working orders, sends a reduce-only market order per open position and
        # re-checks on a timer, re-sending for any remainder, until the strategy is flat.
        self.market_exit()

    # Framework says an exit has begun
    def on_market_exit(self) -> None:
        # Also reached without `_exit`, when the strategy is stopped with `manage_stop=True`
        if self._state is not State.DONE:
            self._state = State.EXITING

    # Late fill after DONE: close it again
    def on_position_opened(self, event: PositionOpened) -> None:
        # Bybit reports order status and fills on separate streams, so a fill can arrive
        # after its order was already reported canceled and the entry written off.
        if self._state is State.DONE:
            self._exit(f"position opened after the strategy had finished: {event.instrument_id}")

    # Framework says the exit loop ended
    def post_market_exit(self) -> None:
        if self._state is State.DONE:
            return  # Stopped after the straddle had already finished, nothing to report

        # Still holding something: try again later
        if self.cache.positions_open(strategy_id=self.id):
            # Timers die with the strategy, so on the shutdown path this retry never fires
            # and `on_stop` reports the open positions instead.
            self.log.error(
                f"Still not flat after {self.config.market_exit_max_attempts} checks, "
                f"trying again in {self.config.exit_retry_secs}s if still running",
            )
            self.clock.set_time_alert(
                name=EXIT_RETRY_TIMER,
                alert_time=self.clock.utc_now() + timedelta(seconds=self.config.exit_retry_secs),
                callback=self._retry_exit,
            )
            return

        # Flat: report the result per leg and in total
        closed = self.cache.positions_closed(strategy_id=self.id)
        pnl = [str(p.realized_pnl) for p in closed]
        report = f"flat, realized PnL per leg (fees included): {pnl or 'no trades'}"
        if closed:
            total = sum((p.realized_pnl.as_decimal() for p in closed), Decimal(0))
            report += f" | NET total: {total} {closed[0].realized_pnl.currency}"
        self._finish(report)

    # Timer callback: run the exit again
    def _retry_exit(self, event: TimeEvent) -> None:
        self.market_exit()

    # Mark finished and stop the status timer
    def _finish(self, reason: str) -> None:
        self._state = State.DONE
        if STATUS_TIMER in self.clock.timer_names:
            self.clock.cancel_timer(STATUS_TIMER)
        self.log.warning(f"Done: {reason}")

    # -- PLAIN-ENGLISH ORDER AND POSITION LINES ---------------------------------------------------

    # Short label such as "call 65000"
    def _leg_label(self, instrument_id: InstrumentId) -> str:
        instrument = self.cache.instrument(instrument_id)
        if isinstance(instrument, CryptoOption):
            kind = "call" if instrument.option_kind == OptionKind.CALL else "put"
            return f"{kind} {instrument.strike_price}"
        return str(instrument_id)

    # An order traded (fully or partly)
    def on_order_filled(self, event: OrderFilled) -> None:
        leg = self._leg_label(event.instrument_id)
        self.log.info(
            f">> FILLED: {event.order_side.name} {event.last_qty} {leg} "
            f"at {event.last_px} (fee {event.commission})",
        )

    # An order was canceled (normal for the unfilled part of IOC)
    def on_order_canceled(self, event: OrderCanceled) -> None:
        self.log.info(f">> CANCELED: order on {self._leg_label(event.instrument_id)}")

    # The exchange refused an order
    def on_order_rejected(self, event: OrderRejected) -> None:
        self.log.warning(
            f">> REJECTED by exchange: {self._leg_label(event.instrument_id)}: {event.reason}",
        )

    # Our own risk checks refused an order
    def on_order_denied(self, event: OrderDenied) -> None:
        self.log.warning(f">> DENIED before sending: {event.reason}")

    # A leg is fully closed: show what it made or lost
    def on_position_closed(self, event: PositionClosed) -> None:
        self.log.info(
            f">> CLOSED {self._leg_label(event.instrument_id)}: sold at {event.avg_px_open}, "
            f"bought back at {event.avg_px_close}, PnL {event.realized_pnl} (fees included)",
        )

    # -- STATUS -----------------------------------------------------------------------------------

    # Every few seconds: one-line progress report
    def _on_status_timer(self, event: TimeEvent) -> None:
        # A long wait for quotes must not end in selling a strike that is no longer at the
        # money. Re-checking here, not on every index price, keeps an index hovering between
        # two strikes from switching subscriptions several times a second.
        if self._state is State.WAITING_FOR_QUOTES:
            self._select_legs()
        if self._state is State.DONE:
            return

        line = (
            f">> STATUS [{self._state.name}] {self._state.value} "
            f"| BTC index={self._reference_price}"
        )
        # Add bid/ask and position of each leg
        if self._call is not None:
            for name, instrument in (("call", self._call), ("put", self._put)):
                quote = self.cache.quote_tick(instrument.id)
                line += f" | {name} {instrument.strike_price} " + (
                    f"bid={quote.bid_price} ask={quote.ask_price}" if quote else "no quote yet"
                )
                line += f" pos={self.portfolio.net_position(instrument.id)}"
        # When open, also show profit/loss progress
        if self._state is State.OPEN:
            buyback = self._buyback_premium()
            if buyback is None:
                # Nothing to buy back from, so the stop-loss cannot be evaluated either
                self.log.warning(f"{line} | no ask on at least one leg, buyback cannot be valued")
                return
            change = (buyback / self._entry_premium - 1) * 100  # % above (+) or below (-) entry
            line += f" | entry={self._entry_premium} buyback={buyback} ({change:+.2f}%)"
            line += f" | stop at {self._stop_level or 'off'}"
            line += f", take-profit at {self._take_level or 'off'}"
        self.log.info(line)


# True if the quote has a real ask price and size
def _has_ask(quote: QuoteTick | None) -> bool:
    return quote is not None and quote.ask_size > 0 and quote.ask_price > 0


# True if the bid can take our whole order
def _can_sell(quote: QuoteTick | None, quantity: Quantity) -> bool:
    # A bid that takes the whole order now, and an ask to value and buy back against later
    return _has_ask(quote) and quote.bid_price > 0 and quote.bid_size >= quantity


# Weighted average price over an order's fills
def _avg_fill_price(order: Order) -> Decimal:
    fills = [event for event in order.events if isinstance(event, OrderFilled)]
    value = sum(fill.last_px.as_decimal() * fill.last_qty.as_decimal() for fill in fills)
    return value / order.filled_qty.as_decimal()
