"""
Run the BTC ATM short straddle on Bybit with NautilusTrader.

    python run.py --level 1                  # naked straddle, no automated exit
    python run.py --level 2                  # plus 1% stop-loss and 2% take-profit
    python run.py --level 2 --env demo       # Bybit demo trading instead of testnet
    python run.py --level 2 --paper          # live Bybit data, fills simulated locally

API keys are read from the environment (or a `.env` file), see `.env.example`.
"""

import argparse
import os
from decimal import Decimal

from dotenv import load_dotenv
from nautilus_trader.adapters.bybit import BYBIT
from nautilus_trader.adapters.bybit import BybitDataClientConfig
from nautilus_trader.adapters.bybit import BybitExecClientConfig
from nautilus_trader.adapters.bybit import BybitLiveDataClientFactory
from nautilus_trader.adapters.bybit import BybitLiveExecClientFactory
from nautilus_trader.adapters.bybit import BybitProductType
from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.core.nautilus_pyo3 import BybitEnvironment
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TraderId

from straddle.strategy import ShortStraddle
from straddle.strategy import ShortStraddleConfig

# Mainnet is deliberately not offered: this is an exercise, not a production system
ENVIRONMENTS = {
    "testnet": (BybitEnvironment.TESTNET, "BYBIT_TESTNET_API_KEY", "BYBIT_TESTNET_API_SECRET"),
    "demo": (BybitEnvironment.DEMO, "BYBIT_DEMO_API_KEY", "BYBIT_DEMO_API_SECRET"),
}

STOP_LOSS_PCT = Decimal("0.01")
TAKE_PROFIT_PCT = Decimal("0.02")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BTC ATM short straddle on Bybit")
    parser.add_argument(
        "--level", type=int, choices=(1, 2), required=True, help="1 = naked, 2 = with SL/TP"
    )
    parser.add_argument(
        "--env", choices=tuple(ENVIRONMENTS), default="testnet", help="Bybit environment"
    )
    parser.add_argument(
        "--paper", action="store_true", help="simulate fills locally, no API key needed"
    )
    parser.add_argument("--quantity", type=Decimal, default=Decimal("0.01"), help="BTC per leg")
    parser.add_argument("--min-hours-to-expiry", type=float, default=12.0)
    parser.add_argument("--keep-open", action="store_true", help="leave positions open on Ctrl-C")
    return parser.parse_args()


def build_node(args: argparse.Namespace) -> TradingNode:
    environment, key_var, secret_var = ENVIRONMENTS[args.env]
    if not args.paper and not (os.getenv(key_var) and os.getenv(secret_var)):
        raise SystemExit(f"Set {key_var} and {secret_var} (see .env.example), or use --paper")

    # Every BTC option plus the BTC linear contracts (for the BTCUSDT index price).
    # Both clients load instruments over REST when they connect.
    btc_instruments = InstrumentProviderConfig(load_all=True, filters={"base_coin": "BTC"})

    data_client = BybitDataClientConfig(
        environment=environment,
        product_types=(BybitProductType.LINEAR, BybitProductType.OPTION),
        instrument_provider=btc_instruments,
    )
    if args.paper:
        exec_client = SandboxExecutionClientConfig(venue="BYBIT", starting_balances=["100000 USDT"])
        exec_factory = SandboxLiveExecClientFactory
    else:
        exec_client = BybitExecClientConfig(
            environment=environment,
            product_types=(BybitProductType.OPTION,),
            instrument_provider=btc_instruments,
        )
        exec_factory = BybitLiveExecClientFactory

    node = TradingNode(
        config=TradingNodeConfig(
            trader_id=TraderId("STRADDLE-001"),
            logging=LoggingConfig(log_level="INFO", log_level_file="DEBUG", log_directory="logs"),
            # On startup, pull open orders and positions from Bybit into the cache
            exec_engine=LiveExecEngineConfig(reconciliation=not args.paper),
            data_clients={BYBIT: data_client},
            exec_clients={BYBIT: exec_client},
        ),
    )
    node.trader.add_strategy(
        ShortStraddle(
            ShortStraddleConfig(
                reference_instrument_id=InstrumentId.from_str(f"BTCUSDT-LINEAR.{BYBIT}"),
                quantity=args.quantity,
                min_hours_to_expiry=args.min_hours_to_expiry,
                stop_loss_pct=STOP_LOSS_PCT if args.level == 2 else None,
                take_profit_pct=TAKE_PROFIT_PCT if args.level == 2 else None,
                # Ctrl-C buys both legs back before the strategy stops, unless --keep-open.
                # The exit loop must fit inside the node's 10s post-stop wait: 16 x 500ms = 8s.
                manage_stop=not args.keep_open,
                market_exit_interval_ms=500,
                market_exit_max_attempts=16,
            ),
        ),
    )
    node.add_data_client_factory(BYBIT, BybitLiveDataClientFactory)
    node.add_exec_client_factory(BYBIT, exec_factory)
    node.build()
    return node


if __name__ == "__main__":
    load_dotenv()
    node = build_node(parse_args())
    try:
        node.run()  # Blocks until Ctrl-C
    finally:
        node.dispose()
