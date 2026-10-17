"""The owner's live setup (3% risk, 6% daily stop, max 3 open, tp_split last) against a fake MT5, with a clock the
test moves. Messages go through the real listener function `to_incoming`, so edits look exactly like live ones.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

from conftest import NOW
from signal_copier.executor.mt5 import Mt5Client, Mt5Executor
from signal_copier.listener.telegram import to_incoming
from signal_copier.notifier import Notifier
from signal_copier.parser import SignalParser
from signal_copier.pipeline import Pipeline
from signal_copier.risk import RiskManager
from signal_copier.settings import Mt5Credentials, Settings
from signal_copier.storage import Storage

CHAT = -1001

AUDUSD_SIGNAL = """RTED Investing - Premium Trading Signals 🏆
AUDUSD
Buy now @ 0.69401
Target Profit 1 @ 0.69446
Target Profit 2 @ 0.69563
Stop Loss @ 0.69311"""
AUDUSD_CLOSE = "‼️ Close AUDUSD Manually now at 0.69530! (+1.43%)"
XAUUSD_MARKET = """RTED Investing - Premium Trading Signals 🏆
XAUUSD
Buy Market Order @ 4479.69
Target Profit 1 @ 4484.30
Target Profit 2 @ 4498.30
Stop Loss @ 4470.38"""
XAUUSD_CLOSE = "‼️ Close XAUUSD Manually now at 4485.10! (+0.50%)"
XAUUSD_SELL_LIMIT = ("RTED Investing - Premium Trading Signals 🏆\nXAUUSD\nSell Limit Order @ 4156.56\n"
                     "Target Profit 1 @ 4153.58\nTarget Profit 2 @ 4142.80\nStop Loss @ 4162.55")
XAUUSD_CANCEL = "Cancel Pending Order on XAUUSD"


def run(coro):
    return asyncio.run(coro)


async def _no_sleep(_):
    return None


def make(fake, **risk) -> Pipeline:
    settings = Settings.model_validate({
        "mode": "live",
        "risk": {"risk_per_trade_pct": "3", "daily_loss_limit_pct": "6", "max_open_positions": 3,
                 "max_spread": {"XAU_USD": "0.50"}, **risk},
        "executor": {"tp_split": "last", "retry_attempts": 4, "retry_base_delay_s": 1.0},
    })
    client = Mt5Client(Mt5Credentials(expect_demo=False), settings.mt5, mt5_module=fake)
    client.sleep = _no_sleep
    storage = Storage(":memory:")
    ex = settings.executor
    executor = Mt5Executor(client, storage, ex.retry_attempts, ex.retry_base_delay_s,
                           ex.pending_order_expiry_minutes, ex.tp_split, settings.parser.max_price_deviation_pct)
    clock = {"t": NOW}
    p = Pipeline(settings, storage, SignalParser(), Notifier(None, None), RiskManager(settings.risk), client,
                 executor, clock=lambda: clock["t"])
    p.test_clock = clock
    return p


def post(p: Pipeline, text: str, mid: int, date: datetime = NOW, reply_to: int | None = None) -> None:
    """A new channel post, delivered 2 s after it was posted."""
    p.test_clock["t"] = date + timedelta(seconds=2)
    tm = NS(id=mid, raw_text=text, date=date, edit_date=None,
            reply_to=NS(reply_to_msg_id=reply_to) if reply_to else None)
    run(p.handle_message(to_incoming(CHAT, tm, edited=False)))


def edit(p: Pipeline, text: str, mid: int, edited_at: datetime, posted: datetime = NOW,
         reply_to: int | None = None) -> None:
    """The channel edits message `mid` (posted at `posted`) at `edited_at`; delivered 2 s later."""
    p.test_clock["t"] = edited_at + timedelta(seconds=2)
    tm = NS(id=mid, raw_text=text, date=posted, edit_date=edited_at,
            reply_to=NS(reply_to_msg_id=reply_to) if reply_to else None)
    run(p.handle_message(to_incoming(CHAT, tm, edited=True)))


def sig(p: Pipeline, mid: int) -> dict:
    return p.storage.get_signal(f"{CHAT}:{mid}:0")


def events(p: Pipeline, kind: str) -> list[tuple]:
    return [(e["decision"], e["reason"]) for e in p.storage.events(kind)]


def bot_positions(fake) -> list:
    return [x for x in fake.positions.values() if x.magic == 770077]
