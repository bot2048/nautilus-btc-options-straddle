"""
Builders for running the strategy inside NautilusTrader's backtest engine.

The backtest engine wires the same Strategy, Cache, RiskEngine, ExecutionEngine and
Portfolio as a live node, with a simulated exchange in place of Bybit. The tests feed
it hand-written index prices and quotes so every scenario is deterministic.
"""

from collections.abc import Callable
from decimal import Decimal

import pandas as pd
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.models import FillModel
from nautilus_trader.backtest.models import LatencyModel
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import BTC
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import IndexPriceUpdate
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.enums import OptionKind
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.instruments import CryptoOption
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Money
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity

from straddle.strategy import ShortStraddle
from straddle.strategy import ShortStraddleConfig

BYBIT = Venue("BYBIT")
REFERENCE_ID = InstrumentId.from_str("BTCUSDT-LINEAR.BYBIT")

SECOND = 1_000_000_000
HOUR = 3_600 * SECOND
T0 = 1_794_268_800 * SECOND  # 2026-11-10 00:00:00 UTC
EXPIRY = T0 + 32 * HOUR  # 2026-11-11 08:00:00 UTC


def make_reference() -> CryptoPerpetual:
    return CryptoPerpetual(
        instrument_id=REFERENCE_ID,
        raw_symbol=Symbol("BTCUSDT"),
        base_currency=BTC,
        quote_currency=USDT,
        settlement_currency=USDT,
        is_inverse=False,
        price_precision=2,
        size_precision=3,
        price_increment=Price.from_str("0.10"),
        size_increment=Quantity.from_str("0.001"),
        ts_event=0,
        ts_init=0,
    )


def make_option(
    kind: OptionKind, strike: int, expiry_ns: int = EXPIRY, tag: str = "11NOV26"
) -> CryptoOption:
    letter = "C" if kind == OptionKind.CALL else "P"
    raw_symbol = f"BTC-{tag}-{strike}-{letter}-USDT"
    return CryptoOption(
        instrument_id=InstrumentId(Symbol(f"{raw_symbol}-OPTION"), BYBIT),
        raw_symbol=Symbol(raw_symbol),
        underlying=BTC,
        quote_currency=USDT,
        settlement_currency=USDT,
        is_inverse=False,
        option_kind=kind,
        strike_price=Price.from_int(strike),
        activation_ns=0,
        expiration_ns=expiry_ns,
        price_precision=0,
        size_precision=2,
        price_increment=Price.from_int(5),
        size_increment=Quantity.from_str("0.01"),
        min_quantity=Quantity.from_str("0.01"),
        ts_event=0,
        ts_init=0,
    )


def make_chain(strikes: tuple[int, ...] = (73000, 74000, 75000)) -> list[CryptoOption]:
    return [
        make_option(kind, strike)
        for strike in strikes
        for kind in (OptionKind.CALL, OptionKind.PUT)
    ]


def index(secs: float, price: str) -> IndexPriceUpdate:
    ts = T0 + int(secs * SECOND)
    return IndexPriceUpdate(REFERENCE_ID, Price.from_str(price), ts, ts)


def quote(
    instrument: CryptoOption,
    secs: float,
    bid: int,
    ask: int,
    bid_size: str = "5.00",
    ask_size: str = "5.00",
) -> QuoteTick:
    ts = T0 + int(secs * SECOND)
    return QuoteTick(
        instrument_id=instrument.id,
        bid_price=Price.from_int(bid),
        ask_price=Price.from_int(ask),
        bid_size=Quantity.from_str(bid_size),
        ask_size=Quantity.from_str(ask_size),
        ts_event=ts,
        ts_init=ts,
    )


def find(options: list[CryptoOption], kind: OptionKind, strike: int) -> CryptoOption:
    return next(o for o in options if o.option_kind == kind and int(o.strike_price) == strike)


def run(
    data: list,
    options: list[CryptoOption] | None = None,
    latency_ms: int = 0,
    fill_model: FillModel | None = None,
    strategy_cls: type[ShortStraddle] = ShortStraddle,
    setup: Callable[[BacktestEngine], None] | None = None,
    **config_kwargs,
) -> tuple[BacktestEngine, ShortStraddle]:
    """
    Run the strategy over `data` and return the engine and strategy for inspection.

    `latency_ms` delays orders on their way to the simulated exchange, so the book can
    move before they arrive. `setup` is called with the engine just before the run.
    """
    engine = BacktestEngine(
        config=BacktestEngineConfig(logging=LoggingConfig(log_level="ERROR")),
    )
    engine.add_venue(
        venue=BYBIT,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=None,
        starting_balances=[Money(100_000, USDT)],
        default_leverage=Decimal(1),
        latency_model=LatencyModel(base_latency_nanos=latency_ms * 1_000_000)
        if latency_ms
        else None,
        fill_model=fill_model,
    )
    engine.add_instrument(make_reference())
    for option in options if options is not None else make_chain():
        engine.add_instrument(option)
    engine.add_data(data)

    strategy = strategy_cls(
        ShortStraddleConfig(reference_instrument_id=REFERENCE_ID, **config_kwargs),
    )
    engine.add_strategy(strategy)
    if setup is not None:
        setup(engine)
    # Run a minute past the last data point so timers set near the end still fire
    end_ns = max(item.ts_init for item in data) + 60 * SECOND
    engine.run(end=pd.Timestamp(end_ns, unit="ns", tz="UTC"))
    return engine, strategy
