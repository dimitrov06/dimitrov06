"""End to end: the app exactly as `run` builds it, on the owner's live setup, against a simulated IC Markets MT5.

- config.yaml is the shipped config.example.yaml with the owner's values (live, 3% risk, 6% daily stop, max 3 open,
  tp_split last, magic 770077), read by settings.load(); app.build_pipeline() builds the Pipeline, Mt5Client,
  Mt5Executor, RiskManager, the SQLite file and the Notifier (no bot token: it only collects the texts), and
  client.connect() runs the live-account checks. "MetaTrader5" is the simulated terminal below.
- Channel posts go through the listener's to_incoming() like live ones (post date, edit date, reply), with the
  real RTED texts from test_real_samples.py.
- The monitor is app._monitor, one round per call (its errors are logged, never raised, as in the app).
- The broker is tests/fake_mt5.StrictMt5 (ticket == order ticket, None reads, refused stops) on a real EUR hedging
  account with 990 EUR. Prices move only when a test says so; the trade server then fills pending orders whose
  price is reached and closes positions at their SL/TP, booking the P/L in EUR as MT5 does.

The only seams: the Pipeline and the executor get a clock the test moves (signal age, the UTC day, the 10 minutes
an order without answer is waited for), and the executor's backoff between polls does not really sleep.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml

import test_resilience as tg_fake
from fake_mt5 import StrictMt5
from owner_setup import XAUUSD_CANCEL, XAUUSD_CLOSE, XAUUSD_SELL_LIMIT
from signal_copier import app
from signal_copier.executor.mt5 import short_tag
from signal_copier.listener.telegram import to_incoming
from signal_copier.settings import load
from test_real_samples import AUDCAD_SIGNAL, AUDUSD_CLOSE, AUDUSD_SIGNAL, XAUUSD_SIGNAL

CHAT = -1001
T0 = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)  # a Thursday, London open
EQUITY = 990.0
RISK_EUR = 29.70  # 3% of 990 EUR
USD_EUR, CAD_EUR = 0.86207, 0.61350  # what 1 USD / 1 CAD is worth in EUR (MT5 gives tick values in EUR)
MAGIC = 770077

# RTED manual format (test_real_samples.test_rted_manual_entry_formats)
XAUUSD_BUY_LIMIT = ("RTED Investing - Premium Trading Signals 🏆\nXAUUSD\nBuy Limit Order @ 4460.00\n"
                    "Target Profit 1 @ 4470.00\nTarget Profit 2 @ 4490.00\nStop Loss @ 4450.00")
XAUUSD_NO_SL = XAUUSD_SIGNAL.replace("\nStop Loss @ 4470.38", "")
SECOND_GOLD = ("RTED Investing - Premium Trading Signals 🏆\nXAUUSD\nBuy now @ 4479.69\nTarget Profit 1 @ 4482.00\n"
               "Target Profit 2 @ 4486.00\nStop Loss @ 4472.00")
AUDCAD_CLOSE = "‼️ Close AUDCAD Manually now at 0.99700! (-0.16%)"


def run(coro):
    return asyncio.run(coro)


async def _no_sleep(_):
    return None


# ------------------------------------------------------------------------------------------------ the broker

class IcMarketsEur(StrictMt5):
    """The owner's account: IC Markets EU, real, EUR, hedging, 990 EUR, Standard (cost in the spread)."""

    def __init__(self):
        super().__init__(demo=False)
        self.balance = EQUITY
        self.symbols = {
            # name: (digits, volume_min, volume_step, tick value in EUR, tick size, filling_mode)
            "XAUUSD": (2, 0.01, 0.01, USD_EUR, 0.01, 2),  # 100 oz: 0.01 = 1 USD per lot
            "AUDUSD": (5, 0.01, 0.01, USD_EUR, 0.00001, 2),  # 100k AUD: 0.00001 = 1 USD per lot
            "AUDCAD": (5, 0.01, 0.01, CAD_EUR, 0.00001, 2),  # 0.00001 = 1 CAD per lot
        }
        self.ticks = {"XAUUSD": (4479.50, 4479.80), "AUDUSD": (0.69400, 0.69402), "AUDCAD": (0.99540, 0.99543)}
        self.before_send: list = []  # hooks: what happens at the trade server just before a request arrives

    def account_info(self):
        a = super().account_info()
        if a is not None:
            a.server, a.currency, a.equity = "ICMarketsEU-Live", "EUR", self.equity_now()
        return a

    def order_send(self, req):
        for hook in list(self.before_send):
            hook(req)
        return super().order_send(req)

    # MT5 returns snapshots (named tuples), never the server's own objects: a later close does not change them
    def positions_get(self, symbol=None, ticket=None):
        res = super().positions_get(symbol=symbol, ticket=ticket)
        return None if res is None else tuple(NS(**vars(p)) for p in res)

    def orders_get(self, ticket=None):
        res = super().orders_get(ticket=ticket)
        return None if res is None else tuple(NS(**vars(o)) for o in res)

    # --- money
    def exit_price(self, p) -> float:
        bid, ask = self.ticks[p.symbol]
        return bid if p.type == self.POSITION_TYPE_BUY else ask

    def pl(self, p, price: float, volume: float) -> float:
        """EUR profit of `volume` lots of position `p` closed at `price`."""
        _, _, _, tick_value, tick_size, _ = self.symbols[p.symbol]
        sign = 1 if p.type == self.POSITION_TYPE_BUY else -1
        return round(sign * round((price - p.price_open) / tick_size) * tick_value * volume, 2)

    def equity_now(self) -> float:
        return round(self.balance + sum(self.pl(p, self.exit_price(p), p.volume) for p in self.positions.values()), 2)

    def _deal(self, position_id, order, entry, price, volume, profit, comment, magic):
        if entry == self.DEAL_ENTRY_OUT:
            profit = self.pl(self.positions[position_id], price, volume)
        super()._deal(position_id, order, entry, price, volume, profit, comment, magic)
        self.deals[-1].commission = 0.0
        self.balance = round(self.balance + profit, 2)

    def deals_pl(self, ticket: int) -> float:
        return round(sum(d.profit + d.commission + d.swap + d.fee for d in self.deals if d.position_id == ticket), 2)

    # --- the market
    def move(self, symbol: str, bid: float, ask: float | None = None) -> None:
        """The price moves: pending orders whose price is reached fill (same ticket), positions whose SL/TP is
        reached close (a stop at the market price, so a gap fills worse than the SL; a TP at its price)."""
        digits = self.symbols[symbol][0]
        old_bid, old_ask = self.ticks[symbol]
        ask = round(bid + (old_ask - old_bid), digits) if ask is None else ask
        self.ticks[symbol] = (bid, ask)
        reached = {self.ORDER_TYPE_BUY_LIMIT: lambda o: ask <= o.price_open,
                   self.ORDER_TYPE_SELL_LIMIT: lambda o: bid >= o.price_open,
                   self.ORDER_TYPE_BUY_STOP: lambda o: ask >= o.price_open,
                   self.ORDER_TYPE_SELL_STOP: lambda o: bid <= o.price_open}
        for o in [o for o in self.orders.values() if o.symbol == symbol]:
            if reached[o.type](o):
                self.fill_pending(o.ticket)
        for p in [p for p in self.positions.values() if p.symbol == symbol]:
            buy, price = p.type == self.POSITION_TYPE_BUY, self.exit_price(p)
            if p.sl and (price <= p.sl if buy else price >= p.sl):
                self._close_at(p, price, f"[sl {p.sl}]")
            elif p.tp and (price >= p.tp if buy else price <= p.tp):
                self._close_at(p, p.tp, f"[tp {p.tp}]")

    def _close_at(self, p, price: float, comment: str) -> None:
        order = next(self._ids)
        self._history_order(order, self.ORDER_STATE_FILLED, comment)
        self._deal(p.ticket, order, self.DEAL_ENTRY_OUT, price, p.volume, 0.0, comment, p.magic)
        del self.positions[p.ticket]

    # --- what the bot did
    def bot_positions(self) -> list:
        return sorted((p for p in self.positions.values() if p.magic == MAGIC), key=lambda p: p.ticket)

    def closes(self, ticket: int) -> list[dict]:
        return [r for r in self.sent if r["action"] == self.TRADE_ACTION_DEAL and r.get("position") == ticket]

    def pending_sends(self) -> list[dict]:
        return [r for r in self.sent if r["action"] == self.TRADE_ACTION_PENDING]


def lots_for_3pct(broker: IcMarketsEur, symbol: str, entry: float, sl: float) -> float:
    """The size whose loss at the SL is 3% of 990 EUR, rounded down to the lot step (from the broker's numbers)."""
    _, _, step, tick_value, tick_size, _ = broker.symbols[symbol]
    loss_per_lot = D(round(abs(entry - sl) / tick_size)) * D(str(tick_value))
    return float((D(str(RISK_EUR)) / loss_per_lot).quantize(D(str(step)), rounding=ROUND_DOWN))


# ------------------------------------------------------------------------------------------------ the app

class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def owner_config(tmp_path: Path, **risk) -> str:
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    cfg = yaml.safe_load(example.read_text(encoding="utf-8"))
    cfg["mode"] = "live"
    cfg["telegram"]["channels"] = [CHAT]
    cfg["risk"].update({"risk_per_trade_pct": 3, "daily_loss_limit_pct": 6, "max_open_positions": 3, **risk})
    cfg["executor"]["tp_split"] = "last"
    cfg["mt5"]["magic"] = MAGIC
    cfg["storage"]["sqlite_path"] = str(tmp_path / "data" / "signal_copier.sqlite")
    cfg["logging"] = {"level": "WARNING", "json": False}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return str(path)


class Bot:
    """settings.load() -> app.build_pipeline() -> client.connect(), as `run` does; then channel posts and monitor
    rounds. `stop()` closes it like `run`'s finally block; a new Bot on the same tmp_path is a restart."""

    def __init__(self, broker: IcMarketsEur, tmp_path: Path, monkeypatch, clock: Clock, **risk):
        monkeypatch.setitem(sys.modules, "MetaTrader5", broker)
        monkeypatch.setenv("LIVE_CONFIRM", "yes")
        for name in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH", "NOTIFY_BOT_TOKEN", "NOTIFY_CHAT_ID"):
            monkeypatch.delenv(name, raising=False)
        settings, secrets = load(owner_config(tmp_path, **risk), env_path=None)
        self.settings = settings
        self.p, self.client, self.notifier = app.build_pipeline(settings, secrets)
        run(self.client.connect())  # refuses a demo account, Algo Trading off, a netting account ...
        self.client.sleep = _no_sleep  # the executor's backoff between polls (~15 s in total)
        self.p.clock = self.p.executor.clock = clock
        self.clock = clock
        self.broker = broker
        self.posts: dict[int, tuple[str, datetime, int | None]] = {}
        self._seen = 0
        self.stopped = False

    # --- the channel
    def post(self, text: str, mid: int, reply_to: int | None = None, delay_s: int = 2) -> None:
        """A new post now, delivered `delay_s` later."""
        posted = self.clock.t
        self.posts[mid] = (text, posted, reply_to)
        self.clock.t = posted + timedelta(seconds=delay_s)
        self._deliver(mid, text, posted, None, reply_to, edited=False)

    def edit(self, mid: int, text: str, delay_s: int = 2) -> None:
        """The channel edits post `mid` now; Telegram keeps its post date."""
        _, posted, reply_to = self.posts[mid]
        edited_at = self.clock.t
        self.posts[mid] = (text, posted, reply_to)
        self.clock.t = edited_at + timedelta(seconds=delay_s)
        self._deliver(mid, text, posted, edited_at, reply_to, edited=True)

    def redeliver(self, mid: int) -> None:
        """Telegram hands the same post over again (reconnect, catch-up)."""
        text, posted, reply_to = self.posts[mid]
        self._deliver(mid, text, posted, None, reply_to, edited=False)

    def _deliver(self, mid, text, posted, edit_date, reply_to, edited) -> None:
        tm = NS(id=mid, raw_text=text, date=posted, edit_date=edit_date,
                reply_to=NS(reply_to_msg_id=reply_to) if reply_to else None)
        run(self.p.handle_message(to_incoming(CHAT, tm, edited=edited)))

    # --- the app's other loops
    def monitor(self) -> None:
        """One round of app._monitor."""
        async def one_round():
            stop = asyncio.Event()
            real = self.p.monitor_once

            async def once():
                try:
                    await real()
                finally:
                    stop.set()

            self.p.monitor_once = once
            try:
                await app._monitor(self.p, self.settings.executor.monitor_interval_s, stop)
            finally:
                del self.p.monitor_once

        run(one_round())

    def command(self, cmd: str) -> str:
        return run(self.p.handle_command(cmd))

    def stop(self) -> None:
        if not self.stopped:
            self.stopped = True
            run(self.client.aclose())
            self.p.storage.close()

    # --- what it did
    def notes(self) -> list[str]:
        """Notifications sent since the last call."""
        new = self.notifier.sent[self._seen:]
        self._seen = len(self.notifier.sent)
        return new

    def sig(self, mid: int) -> dict:
        return self.p.storage.get_signal(f"{CHAT}:{mid}:0")

    def order_statuses(self, mid: int) -> list[str]:
        return [o["status"] for o in self.p.storage.orders_for_signal(f"{CHAT}:{mid}:0")]

    def trades(self, mid: int) -> list[dict]:
        return self.p.storage.trades_for_signal(f"{CHAT}:{mid}:0", open_only=False)


@pytest.fixture
def broker() -> IcMarketsEur:
    return IcMarketsEur()


@pytest.fixture
def clock() -> Clock:
    return Clock(T0)


@pytest.fixture
def start(broker, clock, tmp_path, monkeypatch):
    bots: list[Bot] = []

    def _start(**risk) -> Bot:
        bots.append(Bot(broker, tmp_path, monkeypatch, clock, **risk))
        return bots[-1]

    yield _start
    for b in bots:
        b.stop()


@pytest.fixture
def bot(start) -> Bot:
    return start()


@pytest.fixture
def telethon(monkeypatch):
    """The `telethon` modules the listener imports (not installed here; test_resilience's FakeTg is the client)."""
    tl, ev, ut = (types.ModuleType(n) for n in ("telethon", "telethon.events", "telethon.utils"))
    ev.NewMessage = type("NewMessage", (), {"__init__": lambda self, chats=None: setattr(self, "chats", chats)})
    ev.MessageEdited = type("MessageEdited", (), {"__init__": lambda self, chats=None: setattr(self, "chats", chats)})
    ut.get_peer_id = lambda entity: entity.peer_id
    tl.events, tl.utils = ev, ut
    for name, mod in (("telethon", tl), ("telethon.events", ev), ("telethon.utils", ut)):
        monkeypatch.setitem(sys.modules, name, mod)


# ================================================================== 1. a market signal

@pytest.mark.parametrize("text, symbol, canon, buy, sl, tp2, lots", [
    (AUDUSD_SIGNAL, "AUDUSD", "AUD_USD", True, 0.69311, 0.69563, 0.37),  # "Buy now @ 0.69401"
    (XAUUSD_SIGNAL, "XAUUSD", "XAU_USD", True, 4470.38, 4498.30, 0.03),  # "Buy Market Order @ 4479.69"
    (AUDCAD_SIGNAL, "AUDCAD", "AUD_CAD", False, 0.99723, 0.99179, 0.26),  # "Sell now @ 0.99541"
])


def test_s1_market_signal_opens_one_position_sized_for_3pct_of_the_eur_account(bot, broker, text, symbol, canon,
                                                                                 buy, sl, tp2, lots):
    bot.post(text, 501)

    fill = broker.ticks[symbol][1 if buy else 0]
    assert lots == lots_for_3pct(broker, symbol, fill, sl)
    (req,) = broker.market_sends()
    assert (req["symbol"], req["volume"], req["price"], req["sl"], req["tp"], req["magic"]) == \
        (symbol, lots, fill, sl, tp2, MAGIC)  # one trade, TP = Target Profit 2
    assert req["type"] == (broker.ORDER_TYPE_BUY if buy else broker.ORDER_TYPE_SELL)
    assert req["comment"] == short_tag("sc_n1001_501_0_0") and len(req["comment"]) <= 31
    (pos,) = broker.bot_positions()
    assert (pos.volume, pos.price_open, pos.sl, pos.tp) == (lots, fill, sl, tp2)
    risk_at_sl = -broker.pl(pos, sl, pos.volume)
    assert RISK_EUR + broker.pl(pos, sl, 0.01) < risk_at_sl <= RISK_EUR  # 3%, short only of the lot rounding

    assert bot.sig(501)["status"] == "EXECUTED" and bot.order_statuses(501) == ["FILLED"]
    (t,) = bot.trades(501)
    assert (t["trade_id"], t["state"], D(t["units"])) == (str(pos.ticket), "OPEN", D(str(lots)) * (1 if buy else -1))
    new, opened = bot.notes()
    assert new.startswith(f"📩 Нов сигнал: {canon} {'BUY' if buy else 'SELL'} MARKET @ ")
    assert opened.startswith(f"✅ Отворена {canon} {'BUY' if buy else 'SELL'}: trade {pos.ticket}, {lots} units @ {fill}")


# ================================================================== 2. sell limit: pending -> filled -> TP

def test_s2_sell_limit_waits_fills_and_closes_at_tp2_with_its_eur_pl(bot, broker):
    broker.move("XAUUSD", 4150.00)
    bot.post(XAUUSD_SELL_LIMIT, 1522)  # Sell Limit Order @ 4156.56, TP2 4142.80, SL 4162.55

    lots = lots_for_3pct(broker, "XAUUSD", 4156.56, 4162.55)
    assert lots == 0.05 and broker.market_sends() == []
    (ticket,) = broker.orders
    o = broker.orders[ticket]
    assert (o.type, o.price_open, o.sl, o.tp, o.volume_current, o.magic) == \
        (broker.ORDER_TYPE_SELL_LIMIT, 4156.56, 4162.55, 4142.80, lots, MAGIC)
    assert bot.order_statuses(1522) == ["PENDING"] and bot.trades(1522) == []
    assert "⏳ Чакаща поръчка XAU_USD: 0.05 units @ 4156.56, TP 4142.80" in bot.notes()
    bot.monitor()
    assert bot.notes() == []

    broker.move("XAUUSD", 4156.56)  # the price reaches the entry: the order fills, the position keeps its ticket
    bot.monitor()
    assert bot.notes() == ["✅ Изпълнена чакаща поръчка XAU_USD: 0.05 @ 4156.56"]
    assert bot.order_statuses(1522) == ["FILLED"]
    (t,) = bot.trades(1522)
    assert (t["trade_id"], t["state"]) == (str(ticket), "OPEN")

    bot.post("TP1 hit ✅", 1524, reply_to=1522)  # TP1 is only watched (tp_split last): nothing to do
    assert bot.notes() == ["ℹ️ Каналът съобщава TP_HIT за XAU_USD SELL (msg 1522)"]
    assert len(broker.bot_positions()) == 1

    broker.move("XAUUSD", 4142.50)  # ask 4142.80 = TP2: the broker closes it
    assert broker.bot_positions() == []
    bot.monitor()
    pl = round(1376 * USD_EUR * lots, 2)  # 13.76 in the trade's favour
    assert pl == broker.deals_pl(ticket) == 59.31
    (t,) = bot.trades(1522)
    assert (t["state"], t["close_price"], D(t["realized_pl"])) == ("CLOSED", "4142.8", D("59.31"))
    assert bot.notes() == [f"🏁 Затворена XAU_USD trade {ticket} @ 4142.8, P/L 59.31"]
    assert broker.balance == round(EQUITY + pl, 2)
    bot.monitor()
    assert bot.notes() == []  # reported once


@pytest.mark.parametrize("exit_bid, pl", [
    (4142.50, 59.31),  # ask 4142.80 = TP2
    (4162.40, -26.47),  # ask 4162.70: stopped out 0.15 beyond the SL (a stop fills at the market)
])


def test_s2_sell_limit_that_fills_and_closes_between_two_monitor_rounds_is_reported_with_its_pl(bot, broker,
                                                                                               exit_bid, pl):
    broker.move("XAUUSD", 4150.00)
    bot.post(XAUUSD_SELL_LIMIT, 1522)
    (ticket,) = broker.orders
    bot.notes()
    broker.move("XAUUSD", 4156.56)  # a fast minute: filled ...
    broker.move("XAUUSD", exit_bid)  # ... and closed before the monitor looks
    assert broker.bot_positions() == [] and broker.orders == {}

    bot.monitor()
    assert bot.order_statuses(1522) == ["FILLED"]
    (t,) = bot.trades(1522)
    assert (t["trade_id"], t["state"], D(t["realized_pl"])) == (str(ticket), "CLOSED", D(str(pl)))
    assert pl == broker.deals_pl(ticket)
    filled, closed = bot.notes()
    assert filled == "✅ Изпълнена чакаща поръчка XAU_USD: 0.05 @ 4156.56"
    assert closed.startswith(f"🏁 Затворена XAU_USD trade {ticket} @ ") and closed.endswith(f"P/L {pl}")
    assert bot.p.storage.open_positions() == []  # no slot held by a stale pending row
    bot.monitor()
    assert bot.notes() == []



@pytest.mark.parametrize("failing", ["orders_get", "history_deals_get"])
def test_s2_a_failed_read_never_loses_a_limit_that_filled_and_closed_the_next_round_reports_it(bot, broker,
                                                                                                failing):
    broker.move("XAUUSD", 4150.00)
    bot.post(XAUUSD_SELL_LIMIT, 1522)
    (ticket,) = broker.orders
    broker.move("XAUUSD", 4156.56)
    broker.move("XAUUSD", 4142.50)
    bot.notes()
    broker.fail = {failing: 1}
    bot.monitor()
    assert bot.order_statuses(1522) in (["PENDING"], ["FILLED"])  # never CANCELLED / FAILED on a failed read
    bot.monitor()
    assert bot.order_statuses(1522) == ["FILLED"] and bot.trades(1522)[0]["state"] == "CLOSED"
    assert sum(n.startswith(f"🏁 Затворена XAU_USD trade {ticket}") for n in bot.notes()) == 1

# ================================================================== 3. "Cancel Pending Order on XAUUSD"

def test_s3_cancel_reply_while_pending_removes_the_order(bot, broker):
    broker.move("XAUUSD", 4150.00)
    bot.post(XAUUSD_SELL_LIMIT, 1522)
    (ticket,) = broker.orders
    bot.notes()

    bot.post(XAUUSD_CANCEL, 1523, reply_to=1522)
    assert broker.orders == {} and broker.history_orders[ticket].state == broker.ORDER_STATE_CANCELED
    assert bot.order_statuses(1522) == ["CANCELLED"]
    assert bot.notes() == [f"🔧 CANCEL за XAU_USD SELL (msg 1522):\nотменена чакаща поръчка #{ticket}"]
    assert bot.p.storage.open_positions() == []  # its slots are free

    broker.move("XAUUSD", 4156.56)  # the price gets there later: nothing is left to fill
    bot.monitor()
    assert broker.bot_positions() == [] and bot.notes() == []


@pytest.mark.parametrize("filled", ["before_the_cancel", "while_the_cancel_is_on_its_way"])
def test_s3_cancel_after_the_order_filled_keeps_the_position_and_tells_the_owner(bot, broker, filled):
    broker.move("XAUUSD", 4150.00)
    bot.post(XAUUSD_SELL_LIMIT, 1522)
    (ticket,) = broker.orders
    bot.notes()
    if filled == "before_the_cancel":  # filled between two monitor rounds
        broker.move("XAUUSD", 4156.56)
    else:  # filled at the trade server just before the REMOVE request arrives
        broker.before_send.append(lambda req: req["action"] == broker.TRADE_ACTION_REMOVE
                                  and broker.move("XAUUSD", 4156.56))

    bot.post(XAUUSD_CANCEL, 1523, reply_to=1522)

    (pos,) = broker.bot_positions()
    assert (pos.ticket, pos.volume, pos.sl, pos.tp) == (ticket, 0.05, 4162.55, 4142.80)  # open, with its SL/TP
    assert bot.order_statuses(1522) == ["FILLED"]  # never CANCELLED
    (t,) = bot.trades(1522)
    assert (t["trade_id"], t["state"]) == (str(ticket), "OPEN")
    (note,) = bot.notes()
    assert note.startswith("🔧 CANCEL за XAU_USD SELL (msg 1522):")
    assert f"⚠️ поръчката вече е изпълнена: позиция #{ticket} (0.05 лота) остава отворена" in note

    bot.monitor()  # the monitor keeps it (no "closed", no second "filled")
    assert bot.trades(1522)[0]["state"] == "OPEN" and bot.notes() == []
    bot.post("‼️ Close XAUUSD Manually now at 4150.00! (+0.15%)", 1525, reply_to=1522)
    assert broker.bot_positions() == []


def test_s3_cancel_after_the_order_filled_and_already_closed_reports_it_and_its_pl(bot, broker):
    broker.move("XAUUSD", 4150.00)
    bot.post(XAUUSD_SELL_LIMIT, 1522)
    (ticket,) = broker.orders
    broker.move("XAUUSD", 4156.56)
    broker.move("XAUUSD", 4142.50)  # filled and at TP2 before the bot looked
    bot.notes()
    bot.post(XAUUSD_CANCEL, 1523, reply_to=1522)
    assert broker.market_sends() == [] and bot.order_statuses(1522) == ["FILLED"]
    assert bot.notes()[-1].startswith("🔧 CANCEL за XAU_USD SELL (msg 1522):")
    bot.monitor()
    (t,) = bot.trades(1522)
    assert (t["state"], D(t["realized_pl"])) == ("CLOSED", D("59.31"))
    assert bot.notes()[-1] == f"🏁 Затворена XAU_USD trade {ticket} @ 4142.8, P/L 59.31"


def test_s3_cancel_without_reply_is_not_guessed_when_a_filled_and_a_pending_gold_order_exist(bot, broker):
    """design: the channel may still see as pending an order that IC Markets already filled, so with a filled and a
    pending gold order "Cancel Pending Order on XAUUSD" without reply is ambiguous: nothing is done, ⚠️ notice."""
    broker.move("XAUUSD", 4470.00)
    bot.post(XAUUSD_BUY_LIMIT, 601)  # 4460: pending
    bot.post(XAUUSD_BUY_LIMIT.replace("4460.00", "4469.90").replace("4450.00", "4460.00"), 602)
    broker.move("XAUUSD", 4469.60)  # ask 4469.90: the second one fills
    bot.notes()
    bot.post(XAUUSD_CANCEL, 603)
    assert len(broker.orders) == 1 and len(broker.bot_positions()) == 1
    assert bot.notes()[-1].startswith("⚠️ Команда CANCEL без ясна сделка (ambiguous_target). Не правя нищо.")
    bot.post(XAUUSD_CANCEL, 604, reply_to=601)  # a reply says which one
    assert broker.orders == {} and len(broker.bot_positions()) == 1


# ================================================================== 4. stop to break-even

@pytest.mark.parametrize("text", ["Move SL to BE", "Move SL to entry"])
def test_s4_move_sl_to_be_puts_the_stop_on_the_open_price(bot, broker, text):
    bot.post(XAUUSD_SIGNAL, 505)
    (pos,) = broker.bot_positions()
    broker.move("XAUUSD", 4486.00)  # past TP1
    bot.notes()

    bot.post(text, 506, reply_to=505)
    assert pos.sl == pos.price_open == 4479.80 and pos.tp == 4498.30
    (req,) = broker.sltp_sends()
    assert (req["position"], req["sl"], req["tp"]) == (pos.ticket, 4479.80, 4498.30)
    assert bot.notes() == [f"🔧 MOVE_SL_BE за XAU_USD BUY (msg 505):\nSL → BE 4479.8 (#{pos.ticket})"]

    bot.post("Move SL to BE", 507, reply_to=505)  # said again: no second request, no error
    assert len(broker.sltp_sends()) == 1 and not any(n.startswith("❗") for n in bot.notes())


def test_s4_be_while_the_price_is_behind_the_entry_waits_and_the_monitor_applies_it(bot, broker):
    bot.post(XAUUSD_SIGNAL, 505)
    (pos,) = broker.bot_positions()
    broker.move("XAUUSD", 4477.00)  # below the entry: MT5 would refuse an SL there (10016)
    bot.post("Move SL to BE", 506, reply_to=505)
    assert pos.sl == 4470.38 and broker.sltp_sends() == []
    assert "⏳" in bot.notes()[-1]
    bot.monitor()
    assert pos.sl == 4470.38

    broker.move("XAUUSD", 4483.00)
    bot.monitor()
    assert pos.sl == 4479.80 and len(broker.sltp_sends()) == 1
    assert any("(изчакано)" in n for n in bot.notes())


def test_s4_update_the_sl_to_the_entry_price_is_never_a_wrong_action(bot, broker):
    """design: this copy does not know the phrasing (UNCERTAIN: nothing is done, ⚠️ notice); the owner's PC copy
    adds it as MOVE_SL_BE, and then the stop goes to the open price. It never trades or closes either way."""
    bot.post(XAUUSD_SIGNAL, 505)
    (pos,) = broker.bot_positions()
    broker.move("XAUUSD", 4486.00)
    bot.notes()

    bot.post("Update the SL to the entry price", 506, reply_to=505)
    assert len(broker.market_sends()) == 1 and broker.bot_positions() == [pos]
    note = bot.notes()[-1]
    if pos.sl == pos.price_open:
        assert note.startswith("🔧 MOVE_SL_BE за XAU_USD BUY (msg 505)")
    else:
        assert pos.sl == 4470.38 and broker.sltp_sends() == []
        assert note.startswith("⚠️ Не разбрах съобщението със сигурност") and "НЕ търгувам" in note


# ================================================================== 5. "Close X Manually now at y"

def test_s5_close_manually_closes_the_replied_trade_and_reports_its_pl(bot, broker):
    bot.post(XAUUSD_SIGNAL, 505)
    (pos,) = broker.bot_positions()
    broker.move("XAUUSD", 4485.10)
    bot.notes()

    bot.post(XAUUSD_CLOSE, 506, reply_to=505)  # "‼️ Close XAUUSD Manually now at 4485.10! (+0.50%)"
    assert broker.bot_positions() == []
    (close,) = broker.closes(pos.ticket)
    assert (close["type"], close["volume"], close["price"], close["magic"]) == \
        (broker.ORDER_TYPE_SELL, 0.03, 4485.10, MAGIC)
    assert bot.notes() == [f"🔧 CLOSE за XAU_USD BUY (msg 505):\nзатворена #{pos.ticket} (0.03 лота)"]

    bot.monitor()
    pl = round(530 * USD_EUR * 0.03, 2)  # 4479.80 -> 4485.10
    assert pl == broker.deals_pl(pos.ticket)
    (t,) = bot.trades(505)
    assert (t["state"], D(t["realized_pl"])) == ("CLOSED", D(str(pl)))
    assert bot.notes() == [f"🏁 Затворена XAU_USD trade {pos.ticket} @ 4485.1, P/L {pl}"]

    bot.edit(506, XAUUSD_CLOSE.replace("+0.50%", "+0.51%"))  # the channel edits its close: never a second one
    assert len(broker.closes(pos.ticket)) == 1
    assert "не я повтарям" in bot.notes()[-1]


def test_s5_close_without_reply_closes_the_one_live_trade_of_that_symbol(bot, broker):
    bot.post(AUDUSD_SIGNAL, 501)
    bot.post(XAUUSD_SIGNAL, 505)
    broker.move("AUDUSD", 0.69530)
    bot.post(AUDUSD_CLOSE, 502)  # not a reply
    assert [p.symbol for p in broker.bot_positions()] == ["XAUUSD"]
    assert bot.notes()[-1].startswith("🔧 CLOSE за AUD_USD BUY (msg 501):\nзатворена #")


def test_s5_close_half_then_the_rest_records_one_close_with_the_whole_pl(bot, broker):
    bot.post(AUDUSD_SIGNAL, 501)  # 0.37 lots @ 0.69402
    (pos,) = broker.bot_positions()
    broker.move("AUDUSD", 0.69450)
    bot.post("Close 50% now", 502, reply_to=501)
    assert pos.volume == 0.19  # 0.18 closed (0.185 rounded down to the lot step)
    bot.monitor()
    assert bot.trades(501)[0]["state"] == "OPEN"  # partly closed is still open

    broker.move("AUDUSD", 0.69500)
    bot.post(AUDUSD_CLOSE, 503, reply_to=501)
    assert broker.bot_positions() == []
    bot.monitor()
    (t,) = bot.trades(501)
    pl = round(48 * USD_EUR * 0.18, 2) + round(98 * USD_EUR * 0.19, 2)
    assert t["state"] == "CLOSED" and D(t["realized_pl"]) == D(str(broker.deals_pl(pos.ticket))) == D(str(pl))


def test_s5_close_while_mt5_does_not_answer_is_reported_and_runs_when_the_channel_edits_it(bot, broker):
    """design: a Close that MT5 could not be asked about is FAILED with a ❗ notice (the trade is never marked
    closed); it is not retried on its own, it runs again when the channel edits the message (or the owner closes
    the position in MT5)."""
    bot.post(XAUUSD_SIGNAL, 505)
    (pos,) = broker.bot_positions()
    broker.fail = {"positions_get": 1}
    bot.post(XAUUSD_CLOSE, 506, reply_to=505)
    assert broker.bot_positions() == [pos] and bot.trades(505)[0]["state"] == "OPEN"
    assert bot.notes()[-1].startswith("❗ Грешка при CLOSE за XAU_USD BUY (msg 505)")
    bot.monitor()
    assert broker.bot_positions() == [pos]

    bot.edit(506, XAUUSD_CLOSE + " ✅")
    assert broker.bot_positions() == [] and len(broker.closes(pos.ticket)) == 1


def test_s5_the_owners_own_trade_is_never_touched(bot, broker):
    broker._execute({"action": broker.TRADE_ACTION_DEAL, "symbol": "XAUUSD", "volume": 0.10, "type": 0,
                     "price": 4479.80, "sl": 0.0, "tp": 0.0, "magic": 0, "comment": ""})  # opened by hand in MT5
    (manual,) = broker.positions.values()
    bot.post(XAUUSD_SIGNAL, 505)
    bot.post(XAUUSD_CLOSE, 506)  # not a reply: the bot's one gold trade, never the manual one
    assert list(broker.positions.values()) == [manual] and broker.closes(manual.ticket) == []
    bot.post(XAUUSD_CLOSE, 507)
    assert list(broker.positions.values()) == [manual]


def test_s5_close_after_the_trade_already_hit_tp2_sends_nothing(bot, broker):
    bot.post(XAUUSD_SIGNAL, 505)
    (pos,) = broker.bot_positions()
    broker.move("XAUUSD", 4498.30)  # TP2 at the broker, the monitor has not looked yet
    bot.post(XAUUSD_CLOSE, 506, reply_to=505)
    assert broker.closes(pos.ticket) == []
    assert bot.notes()[-1] == "🔧 CLOSE за XAU_USD BUY (msg 505):\nняма отворени сделки/поръчки"
    bot.monitor()
    assert bot.trades(505)[0]["state"] == "CLOSED"


def test_s5_close_whose_answer_is_lost_is_never_sent_twice(bot, broker):
    """design: MT5 executed the close but answered None: the bot says ❗ (it cannot know), never closes twice, and
    the monitor then reports the close with its P/L."""
    bot.post(XAUUSD_SIGNAL, 505)
    (pos,) = broker.bot_positions()
    broker.script = ["lost"]
    bot.post(XAUUSD_CLOSE, 506, reply_to=505)
    assert broker.bot_positions() == [] and len(broker.closes(pos.ticket)) == 1
    assert bot.notes()[-1].startswith("❗ Грешка при CLOSE за XAU_USD BUY (msg 505)")
    bot.monitor()
    assert bot.trades(505)[0]["state"] == "CLOSED" and bot.notes()[-1].startswith("🏁 Затворена XAU_USD")
    bot.edit(506, XAUUSD_CLOSE + " ✅")  # a FAILED close runs again on an edit: nothing left to close
    assert len(broker.closes(pos.ticket)) == 1


# ================================================================== 6. no answer from order_send

def test_s6_lost_answer_and_failing_reads_give_exactly_one_tracked_position(bot, broker):
    broker.script = ["lost"]  # executed, but order_send returns None
    broker.fail = {"positions_get": 3}  # and the next lookups return None too
    bot.post(XAUUSD_SIGNAL, 505)

    assert len(broker.market_sends()) == 1
    (pos,) = broker.bot_positions()
    assert bot.order_statuses(505) == ["FILLED"] and bot.sig(505)["status"] == "EXECUTED"
    (t,) = bot.trades(505)
    assert (t["trade_id"], t["state"]) == (str(pos.ticket), "OPEN")
    assert any(n.startswith(f"✅ Отворена XAU_USD BUY: trade {pos.ticket}") for n in bot.notes())

    broker.fail = {"positions_get": 1, "account_info": 2, "history_deals_get": 1}
    bot.monitor()  # MT5 does not answer: proves nothing, the app's loop survives it
    bot.monitor()
    bot.redeliver(505)
    assert bot.trades(505)[0]["state"] == "OPEN" and len(broker.market_sends()) == 1

    bot.post(XAUUSD_CLOSE, 506, reply_to=505)
    assert broker.bot_positions() == [] and len(broker.closes(pos.ticket)) == 1


def test_s6_lost_answer_during_an_ipc_outage_stays_unknown_then_is_adopted_and_closable(bot, broker):
    real_send = broker.order_send

    def send_then_ipc_down(req):
        res = real_send(req)
        broker.order_send = real_send
        broker.ipc_down = True  # the terminal's IPC drops for longer than the bot polls (~15 s)
        return res

    broker.script = ["lost"]
    broker.order_send = send_then_ipc_down
    bot.post(XAUUSD_SIGNAL, 505)

    assert len(broker.market_sends()) == 1 and len(broker.bot_positions()) == 1  # it did execute
    assert bot.order_statuses(505) == ["SENT"] and bot.sig(505)["status"] == "EXECUTED"
    assert any("НЕ я пращам повторно" in n for n in bot.notes())
    bot.redeliver(505)
    bot.monitor()  # still down
    assert bot.order_statuses(505) == ["SENT"] and len(broker.market_sends()) == 1

    broker.ipc_down = False
    bot.monitor()
    (pos,) = broker.bot_positions()
    assert bot.order_statuses(505) == ["FILLED"] and bot.trades(505)[0]["trade_id"] == str(pos.ticket)
    assert any(n.startswith(f"✅ Поръчката без отговор е изпълнена в MT5: XAU_USD trade {pos.ticket}")
               for n in bot.notes())
    bot.post(XAUUSD_CLOSE, 506, reply_to=505)
    assert broker.bot_positions() == [] and len(broker.market_sends()) == 1


def test_s6_no_answer_and_not_executed_is_never_resent_and_expires_after_ten_minutes(bot, broker, clock):
    broker.script = ["none"]  # never reached the server, returns None
    bot.post(XAUUSD_SIGNAL, 505)

    assert len(broker.market_sends()) == 1 and broker.bot_positions() == []
    assert bot.order_statuses(505) == ["SENT"]
    assert any(n.startswith("❗") and "НЕ я пращам повторно" in n for n in bot.notes())
    assert len(bot.p.storage.open_positions(include_pending=False)) == 1  # may be a position: holds its slot
    bot.redeliver(505)
    bot.edit(505, XAUUSD_SIGNAL.replace("4498.30", "4499.30"))
    assert len(broker.market_sends()) == 1

    for minutes in (1, 6):
        clock.t = T0 + timedelta(minutes=minutes)
        bot.monitor()
    assert bot.order_statuses(505) == ["SENT"]
    clock.t = T0 + timedelta(minutes=11)  # 10 minutes in a row without any trace, every read answered
    bot.monitor()
    assert bot.order_statuses(505) == ["FAILED"] and bot.sig(505)["status"] == "FAILED"
    assert any(n.startswith("⌛ Поръчката без отговор XAU_USD") for n in bot.notes())
    assert bot.p.storage.open_positions() == []
    for minutes in (12, 30, 90):
        clock.t = T0 + timedelta(minutes=minutes)
        bot.monitor()
    assert len(broker.market_sends()) == 1  # never sent again

    bot.post(AUDUSD_SIGNAL, 510)  # its slot is free
    assert bot.sig(510)["status"] == "EXECUTED" and len(broker.bot_positions()) == 1


def test_s6_lost_answer_on_a_sell_limit_gives_exactly_one_pending_order_that_is_then_followed(bot, broker):
    broker.move("XAUUSD", 4150.00)
    broker.script = ["lost"]
    bot.post(XAUUSD_SELL_LIMIT, 1522)
    (ticket,) = broker.orders
    assert len(broker.pending_sends()) == 1 and bot.order_statuses(1522) == ["PENDING"]
    assert bot.notes()[-1].startswith("⏳ Чакаща поръчка XAU_USD: 0.05 units @ 4156.56")

    broker.move("XAUUSD", 4156.56)
    bot.monitor()
    assert bot.notes() == ["✅ Изпълнена чакаща поръчка XAU_USD: 0.05 @ 4156.56"]
    assert bot.trades(1522)[0]["trade_id"] == str(ticket)


def test_s6_an_order_without_answer_is_adopted_after_a_restart(start, broker, clock):
    a = start()
    real_send = broker.order_send

    def send_then_ipc_down(req):
        res = real_send(req)
        broker.order_send = real_send
        broker.ipc_down = True
        return res

    broker.script = ["lost"]
    broker.order_send = send_then_ipc_down
    a.post(XAUUSD_SIGNAL, 505)
    assert a.order_statuses(505) == ["SENT"]
    assert "MT5 недостъпен" in run(a.p.status_text())
    a.stop()

    broker.ipc_down = False
    clock.t += timedelta(minutes=3)
    b = start()
    b.monitor()
    (pos,) = broker.bot_positions()
    assert b.order_statuses(505) == ["FILLED"] and b.trades(505)[0]["trade_id"] == str(pos.ticket)
    b.post(XAUUSD_CLOSE, 506, reply_to=505)
    assert broker.bot_positions() == [] and len(broker.market_sends()) == 1



@pytest.mark.parametrize("command", ["close", "cancel"])
def test_s6_a_command_that_arrives_while_the_order_is_unknown_is_applied_when_the_order_turns_up(bot, broker,
                                                                                                  command):
    if command == "close":  # a market order answered 10012 (timeout) that the server fills a bit later
        mid, text = 505, XAUUSD_CLOSE
        broker.script = ["timeout_late"]
        bot.post(XAUUSD_SIGNAL, mid)
    else:  # the same for a sell limit: the channel cancels it before it shows up
        mid, text = 1522, XAUUSD_CANCEL
        broker.move("XAUUSD", 4150.00)
        broker.script = ["timeout_late"]
        bot.post(XAUUSD_SELL_LIMIT, mid)
    assert bot.order_statuses(mid) == ["SENT"]
    bot.notes()

    bot.post(text, 1600, reply_to=mid)
    (note,) = bot.notes()
    assert "⏳" in note and "няма отворени" not in note
    broker.settle()  # the trade server finishes the request after all
    bot.monitor()
    assert broker.bot_positions() == [] and broker.orders == {}  # what the channel asked for, once it is there
    assert any("(изчакано)" in n for n in bot.notes())
    bot.monitor()  # done once; a closed trade is then reported with its P/L
    assert bot.p.storage.follow_ups_with_status("WAITING") == []
    notes = bot.notes()
    if command == "close":
        (closed,) = notes
        assert closed.startswith("🏁 Затворена XAU_USD trade") and bot.trades(mid)[0]["state"] == "CLOSED"
    else:
        assert notes == []


def test_s6_a_close_waiting_for_an_order_that_never_shows_up_ends_when_the_order_is_given_up(bot, broker, clock):
    broker.script = ["timeout"]  # 10012 and never executed
    bot.post(XAUUSD_SIGNAL, 505)
    bot.post(XAUUSD_CLOSE, 506, reply_to=505)
    assert "⏳" in bot.notes()[-1]
    for minutes in (1, 6, 11):
        clock.t = T0 + timedelta(minutes=minutes)
        bot.monitor()
    assert bot.order_statuses(505) == ["FAILED"] and bot.p.storage.follow_ups_with_status("WAITING") == []
    assert broker.bot_positions() == [] and len(broker.market_sends()) == 1
    assert bot.notes()[-1].startswith("🔧 CLOSE за XAU_USD BUY (msg 505) (изчакано):")

# ================================================================== 7. duplicates and edits

def test_s7_duplicate_delivery_never_trades_twice_and_an_edit_amends_the_live_levels(bot, broker):
    bot.post(XAUUSD_SIGNAL, 505)
    (pos,) = broker.bot_positions()
    bot.redeliver(505)
    bot.redeliver(505)
    assert len(broker.market_sends()) == 1
    bot.notes()

    edited = XAUUSD_SIGNAL.replace("Stop Loss @ 4470.38", "Stop Loss @ 4472.38") \
                          .replace("Target Profit 2 @ 4498.30", "Target Profit 2 @ 4500.30")
    bot.edit(505, edited)
    assert (pos.sl, pos.tp) == (4472.38, 4500.30) and len(broker.market_sends()) == 1
    s = bot.sig(505)
    assert s["sl"] == "4472.38" and json.loads(s["tps"]) == ["4484.30", "4500.30"]
    assert bot.notes() == ["✏️ Редактиран сигнал XAU_USD: SL 4470.38 → 4472.38, TP 4484.30, 4498.30 → 4484.30, "
                           f"4500.30\n#{pos.ticket}: SL 4472.38, TP 4500.3"]
    bot.redeliver(505)  # the edit delivered again
    assert len(broker.sltp_sends()) == 1

    bot.edit(505, edited.replace("Stop Loss @ 4472.38", "Stop Loss @ 4465.38"))  # wider: never applied
    assert pos.sl == 4472.38 and "не го разхлабвам" in bot.notes()[-1]


def test_s7_an_sl_added_by_an_edit_right_after_the_post_is_traded(bot, broker):
    bot.post(XAUUSD_NO_SL, 505)
    assert bot.sig(505)["reason"] == "missing_sl" and broker.sent == []
    bot.edit(505, XAUUSD_SIGNAL)  # 2 s later
    (pos,) = broker.bot_positions()
    assert pos.sl == 4470.38 and bot.sig(505)["status"] == "EXECUTED"


def test_s7_an_edit_never_opens_a_signal_that_was_rejected_or_is_old(bot, broker, clock):
    bot.post(XAUUSD_SIGNAL, 505, delay_s=90)  # reached the bot 90 s after the post
    assert bot.sig(505)["reason"] == "signal_too_old:90s"
    bot.edit(505, XAUUSD_SIGNAL.replace("4498.30", "4499.30"))
    assert broker.sent == [] and "Не търгувам" in bot.notes()[-1]

    bot.post(XAUUSD_NO_SL, 506)
    clock.t += timedelta(minutes=5)
    bot.edit(506, XAUUSD_SIGNAL)  # the SL comes 5 minutes later: the entry is not fresh any more
    assert broker.sent == [] and bot.sig(506)["status"] == "REJECTED"
    assert "публикуван е преди" in bot.notes()[-1]

    bot.post(AUDUSD_SIGNAL, 501)  # traded, then closed at TP2
    broker.move("AUDUSD", 0.69563)
    bot.monitor()
    assert broker.bot_positions() == []
    bot.edit(501, AUDUSD_SIGNAL + "\n\nTP2 hit ✅✅")
    bot.edit(501, AUDUSD_SIGNAL.replace("0.69563", "0.69600"))
    assert len(broker.market_sends()) == 1 and broker.bot_positions() == []


def test_s7_an_edit_back_to_the_posted_levels_is_applied_too(bot, broker):
    bot.post(XAUUSD_SIGNAL, 505)
    (pos,) = broker.bot_positions()
    bot.edit(505, XAUUSD_SIGNAL.replace("4498.30", "4500.30"))
    assert pos.tp == 4500.30
    bot.notes()

    bot.edit(505, XAUUSD_SIGNAL)  # the channel goes back to the TP2 it posted
    assert pos.tp == 4498.30 and json.loads(bot.sig(505)["tps"])[-1] == "4498.30"
    assert bot.notes() == ["✏️ Редактиран сигнал XAU_USD: SL 4470.38 → 4470.38, TP 4484.30, 4500.30 → 4484.30, "
                           f"4498.30\n#{pos.ticket}: SL 4470.38, TP 4498.3"]
    bot.redeliver(505)  # that version delivered again: nothing more
    assert len(broker.sltp_sends()) == 2 and len(broker.market_sends()) == 1

    tighter = XAUUSD_SIGNAL.replace("Stop Loss @ 4470.38", "Stop Loss @ 4472.38")
    bot.edit(505, tighter)
    assert pos.sl == 4472.38
    bot.edit(505, XAUUSD_SIGNAL)  # and back to the posted SL: wider, so refused, but never in silence
    assert pos.sl == 4472.38 and "не го разхлабвам" in bot.notes()[-1]


# ================================================================== 8. restart

def test_s8_after_a_restart_the_open_position_and_the_pending_order_are_still_managed(start, broker, clock):
    broker.move("XAUUSD", 4150.00)
    a = start()
    a.post(AUDUSD_SIGNAL, 501)
    a.post(XAUUSD_SELL_LIMIT, 1522)
    (aud,) = broker.bot_positions()
    (limit,) = broker.orders
    a.stop()

    broker.move("XAUUSD", 4156.56)  # the limit fills while the bot is down
    clock.t += timedelta(minutes=20)
    b = start()  # same config.yaml, same SQLite file, same terminal
    b.posts.update(a.posts)
    b.monitor()
    assert b.notes() == ["✅ Изпълнена чакаща поръчка XAU_USD: 0.05 @ 4156.56"]
    assert b.order_statuses(1522) == ["FILLED"] and b.trades(1522)[0]["trade_id"] == str(limit)

    b.redeliver(501)  # re-delivered after the restart: duplicates
    b.redeliver(1522)
    assert len(broker.market_sends()) == 1 and len(broker.pending_sends()) == 1

    broker.move("XAUUSD", 4150.00)
    b.post("Move SL to BE", 1530, reply_to=1522)
    assert broker.positions[limit].sl == 4156.56
    b.post(AUDUSD_CLOSE, 1531, reply_to=501)
    assert [p.ticket for p in broker.bot_positions()] == [limit] and aud.ticket not in broker.positions
    b.monitor()
    assert b.trades(501)[0]["state"] == "CLOSED" and b.trades(1522)[0]["state"] == "OPEN"


def test_s8_a_cancel_after_a_restart_removes_the_pending_order(start, broker, clock):
    broker.move("XAUUSD", 4150.00)
    a = start()
    a.post(XAUUSD_SELL_LIMIT, 1522)
    (ticket,) = broker.orders
    a.stop()

    clock.t += timedelta(minutes=5)
    b = start()
    b.post(XAUUSD_CANCEL, 1523, reply_to=1522)
    assert broker.orders == {} and b.order_statuses(1522) == ["CANCELLED"]
    assert b.notes()[-1] == f"🔧 CANCEL за XAU_USD SELL (msg 1522):\nотменена чакаща поръчка #{ticket}"


def test_s8_a_break_even_waiting_for_the_price_is_applied_after_a_restart(start, broker, clock):
    a = start()
    a.post(XAUUSD_SIGNAL, 505)
    (pos,) = broker.bot_positions()
    broker.move("XAUUSD", 4477.00)
    a.post("Move SL to BE", 506, reply_to=505)
    assert pos.sl == 4470.38
    a.stop()

    broker.move("XAUUSD", 4483.00)
    clock.t += timedelta(minutes=2)
    b = start()
    b.monitor()
    assert pos.sl == 4479.80 and any("(изчакано)" in n for n in b.notes())


def test_s8_a_pending_order_that_filled_and_stopped_out_while_the_bot_was_down_is_reported(start, broker, clock):
    broker.move("XAUUSD", 4150.00)
    a = start()
    a.post(XAUUSD_SELL_LIMIT, 1522)
    (ticket,) = broker.orders
    a.stop()

    broker.move("XAUUSD", 4156.56)
    broker.move("XAUUSD", 4162.25)  # ask 4162.55 = SL
    clock.t += timedelta(hours=1)
    b = start()
    b.monitor()
    pl = round(-599 * USD_EUR * 0.05, 2)
    (t,) = b.trades(1522)
    assert (t["state"], D(t["realized_pl"])) == ("CLOSED", D(str(pl))) and b.order_statuses(1522) == ["FILLED"]
    assert b.notes()[-1] == f"🏁 Затворена XAU_USD trade {ticket} @ 4162.55, P/L {pl}"


def test_s8_follow_ups_posted_while_the_bot_was_down_are_applied_at_the_restart(start, broker, clock, telethon):
    history = {CHAT: []}
    a = start()
    clock.t = T0 + timedelta(seconds=2)
    run(tg_fake.session(a.p, history, live=[tg_fake.tg(505, XAUUSD_SIGNAL, date=T0)], now=clock.t))
    (pos,) = broker.bot_positions()
    a.stop()

    broker.move("XAUUSD", 4486.00)  # while the bot is down: TP1, the channel moves the SL to BE, a new signal
    history[CHAT] += [tg_fake.tg(506, "TP1 hit ✅", date=T0 + timedelta(minutes=10), reply_to=505),
                      tg_fake.tg(507, "Move SL to BE", date=T0 + timedelta(minutes=11), reply_to=505),
                      tg_fake.tg(508, AUDUSD_SIGNAL, date=T0 + timedelta(minutes=20))]
    clock.t = T0 + timedelta(minutes=40)
    b = start()
    run(tg_fake.session(b.p, history, now=clock.t))
    assert pos.sl == 4479.80  # BE applied at the start
    assert b.sig(508)["reason"].startswith("signal_too_old") and len(broker.market_sends()) == 1
    assert any("Докато ботът беше спрян" in n and "3 съобщения" in n for n in b.notes())


# ================================================================== 9. limits: max open, daily stop, /stop

def open_three_and_a_pending(bot):
    bot.post(XAUUSD_BUY_LIMIT, 601)  # pending, 0.03 lots @ 4460
    bot.post(AUDUSD_SIGNAL, 602)
    bot.post(AUDCAD_SIGNAL, 603)
    bot.post(AUDUSD_SIGNAL, 604)  # the channel posts AUDUSD again: a second AUDUSD trade (2 per symbol)


def test_s9_pending_orders_do_not_count_towards_max_open_by_default(bot, broker):
    open_three_and_a_pending(bot)
    assert len(broker.bot_positions()) == 3 and len(broker.orders) == 1
    assert bot.sig(604)["status"] == "EXECUTED"

    bot.post(XAUUSD_SIGNAL, 605)  # a 4th open position: no
    assert bot.sig(605)["reason"] == "max_open_positions" and len(broker.market_sends()) == 3
    assert bot.notes()[-1] == "🚫 Отхвърлен сигнал XAU_USD BUY: max_open_positions"

    # design (owner decision): the pending order that fills later is a 4th open position
    broker.move("XAUUSD", 4459.70)  # ask 4460.00
    bot.monitor()
    assert len(broker.bot_positions()) == 4
    assert bot.notes() == ["✅ Изпълнена чакаща поръчка XAU_USD: 0.03 @ 4460.0"]
    bot.post(AUDCAD_SIGNAL, 606)
    assert bot.sig(606)["reason"] == "max_open_positions"


def test_s9_pending_orders_count_towards_max_open_when_configured(start, broker):
    bot = start(count_pending_in_max_open=True)
    open_three_and_a_pending(bot)
    assert bot.sig(604)["reason"] == "max_open_positions"
    assert len(broker.bot_positions()) == 2 and len(broker.orders) == 1


def test_s9_daily_stop_at_6pct_halts_new_trades_cancels_pending_orders_and_keeps_managing(bot, broker, clock):
    open_three_and_a_pending(bot)
    bot.notes()
    broker.move("AUDUSD", 0.69311)  # both AUDUSD trades are stopped out
    broker.move("AUDCAD", 0.99697)  # ask 0.99700: AUDCAD against us
    assert len(broker.bot_positions()) == 1
    assert (EQUITY - broker.equity_now()) / EQUITY >= 0.06

    bot.monitor()
    notes = bot.notes()
    assert any(n.startswith("🛑 Достигнат дневен лимит на загуба (6%)") for n in notes)
    assert broker.orders == {} and bot.order_statuses(601) == ["CANCELLED"]
    assert any(n.startswith("🛑 Дневен лимит: чакащите поръчки\nотменена чакаща поръчка #") for n in notes)
    assert [p.symbol for p in broker.bot_positions()] == ["AUDCAD"]  # kept, with its SL/TP

    broker.move("AUDUSD", 0.69400)
    bot.post(AUDUSD_SIGNAL, 605)
    assert bot.sig(605)["reason"] == "halted:daily_loss_limit" and len(broker.market_sends()) == 3
    bot.post(AUDCAD_CLOSE, 606, reply_to=603)  # follow-ups still work
    assert broker.bot_positions() == []
    bot.monitor()

    clock.t = datetime(2026, 10, 16, 7, 0, tzinfo=timezone.utc)  # the next UTC day
    bot.post(AUDUSD_SIGNAL, 607)
    assert bot.sig(607)["status"] == "EXECUTED" and len(broker.bot_positions()) == 1


def test_s9_stop_command_blocks_new_trades_while_follow_ups_still_work(bot, broker):
    bot.post(XAUUSD_SIGNAL, 505)
    (pos,) = broker.bot_positions()
    assert bot.command("/stop").startswith("⛔ Спрях нови сделки")
    assert "спрян (manual_stop)" in run(bot.p.status_text())

    bot.post(AUDUSD_SIGNAL, 501)
    assert bot.sig(501)["reason"] == "halted:manual_stop" and len(broker.market_sends()) == 1
    assert bot.notes()[-1] == "🚫 Отхвърлен сигнал AUD_USD BUY: halted:manual_stop"
    broker.move("XAUUSD", 4486.00)
    bot.post("Move SL to BE", 506, reply_to=505)
    assert pos.sl == 4479.80
    bot.post(XAUUSD_CLOSE, 507, reply_to=505)
    assert broker.bot_positions() == []

    assert bot.command("/resume").startswith("▶️ Пуснах отново")
    bot.post(AUDUSD_SIGNAL, 502)
    assert bot.sig(502)["status"] == "EXECUTED" and len(broker.bot_positions()) == 1


def test_s9_daily_stop_reached_between_monitor_rounds_stops_the_next_signal_and_the_pending_order(bot, broker):
    open_three_and_a_pending(bot)
    broker.move("AUDUSD", 0.69311)  # both AUDUSD trades stopped out
    broker.move("AUDCAD", 0.99697)
    broker.move("AUDUSD", 0.69400)
    bot.notes()

    bot.post(XAUUSD_SIGNAL, 605)  # before any monitor round
    assert bot.sig(605)["reason"] == "daily_loss_limit" and len(broker.market_sends()) == 3
    notes = bot.notes()
    assert "🚫 Отхвърлен сигнал XAU_USD BUY: daily_loss_limit" in notes
    assert any(n.startswith("🛑 Достигнат дневен лимит на загуба (6%)") for n in notes)
    assert broker.orders == {} and bot.order_statuses(601) == ["CANCELLED"]


def test_s9_the_daily_stop_and_stop_command_survive_a_restart(start, broker, clock):
    a = start()
    open_three_and_a_pending(a)
    broker.move("AUDUSD", 0.69311)
    broker.move("AUDCAD", 0.99697)
    a.monitor()
    assert a.p.halted_reason() == "daily_loss_limit"
    a.stop()

    clock.t += timedelta(minutes=30)
    broker.move("AUDUSD", 0.69400)
    b = start()
    b.post(AUDUSD_SIGNAL, 605)
    assert b.sig(605)["reason"] == "halted:daily_loss_limit"
    b.command("/stop")
    b.stop()

    clock.t = datetime(2026, 10, 16, 7, 0, tzinfo=timezone.utc)  # the next day: the daily stop is over ...
    c = start()
    c.post(AUDUSD_SIGNAL, 606)
    assert c.sig(606)["reason"] == "halted:manual_stop"  # ... but /stop holds until /resume
    c.command("/resume")
    c.post(AUDUSD_SIGNAL, 607)
    assert c.sig(607)["status"] == "EXECUTED"


# ================================================================== 10. too late

def test_s10_signal_delivered_too_late_is_skipped_with_a_notice(bot, broker):
    bot.post(XAUUSD_SIGNAL, 505, delay_s=75)
    assert broker.sent == [] and bot.sig(505)["status"] == "REJECTED"
    assert bot.notes()[-1] == "🚫 Отхвърлен сигнал XAU_USD BUY: signal_too_old:75s"


@pytest.mark.parametrize("ask, reason", [
    (4483.00, "price_moved_too_far:3.31>2.793"),  # 3.31 towards TP, more than 30% of the 9.31 SL distance
    (4484.50, "price_beyond_tp1"),
])


def test_s10_price_already_ran_towards_tp_is_skipped_with_a_notice(bot, broker, ask, reason):
    broker.move("XAUUSD", round(ask - 0.30, 2), ask)
    bot.post(XAUUSD_SIGNAL, 505)
    assert broker.sent == [] and bot.sig(505)["reason"] == reason
    assert bot.notes()[-1] == f"🚫 Отхвърлен сигнал XAU_USD BUY: {reason}"


def test_s10_late_entry_after_an_adverse_move_is_taken_with_at_most_3pct_risk(bot, broker):
    """design (RTED manual): price against the trade but before the SL is a better entry, not a skip."""
    broker.move("XAUUSD", 4474.70)  # ask 4475.00: 4.69 below the posted entry, 4.62 above the SL
    bot.post(XAUUSD_SIGNAL, 505)
    (pos,) = broker.bot_positions()
    assert pos.volume == lots_for_3pct(broker, "XAUUSD", 4475.00, 4470.38) == 0.07
    assert -broker.pl(pos, pos.sl, pos.volume) <= RISK_EUR


# ================================================================== 11. which gold trade does "Close" mean?

def test_s11_close_without_reply_is_not_guessed_until_exactly_one_trade_is_live(bot, broker):
    bot.post(XAUUSD_SIGNAL, 505)
    bot.post(SECOND_GOLD, 506)
    first, second = broker.bot_positions()
    bot.notes()

    bot.post(XAUUSD_CLOSE, 507)  # no reply, two gold trades live
    assert len(broker.bot_positions()) == 2
    assert bot.notes()[-1].startswith("⚠️ Команда CLOSE без ясна сделка (ambiguous_target). Не правя нищо.")

    broker.move("XAUUSD", 4486.00)  # the second reaches its TP2
    assert broker.bot_positions() == [first]
    bot.post(XAUUSD_CLOSE, 508)  # the bot has not seen that close yet: still two in its books
    assert broker.bot_positions() == [first] and "ambiguous_target" in bot.notes()[-1]

    bot.monitor()
    bot.post(XAUUSD_CLOSE, 509)
    assert broker.bot_positions() == []
    assert bot.trades(505)[0]["state"] == "OPEN"  # until the monitor records the close
    bot.monitor()
    assert [t["state"] for t in bot.trades(505) + bot.trades(506)] == ["CLOSED", "CLOSED"]


def test_s11_a_reply_closes_only_the_replied_trade(bot, broker):
    bot.post(XAUUSD_SIGNAL, 505)
    bot.post(SECOND_GOLD, 506)
    first, second = broker.bot_positions()
    bot.post(XAUUSD_CLOSE, 507, reply_to=506)
    assert broker.bot_positions() == [first]


def test_s11_a_close_replying_to_a_signal_the_bot_did_not_trade_never_closes_another_gold_trade(bot, broker):
    bot.post(XAUUSD_SIGNAL, 505)
    (pos,) = broker.bot_positions()
    bot.post(SECOND_GOLD, 506, delay_s=90)  # reached the bot too late: not traded
    assert bot.sig(506)["reason"] == "signal_too_old:90s"
    bot.post(XAUUSD_CLOSE, 507, reply_to=506)  # the channel closes *that* one
    assert broker.bot_positions() == [pos]
    assert bot.notes()[-1].startswith("⚠️ Команда CLOSE без ясна сделка (target_signal_not_found)")


# ================================================================== 12. the terminal cannot trade

def test_s12_algo_trading_switched_off_is_reported_once_and_nothing_trades_until_it_is_back(bot, broker):
    bot.monitor()
    broker.algo_trading = False  # the owner (or MT5 after an update) switched Algo Trading off
    bot.monitor()
    assert bot.notes() == ["🚫 MT5 не може да търгува: бутонът Algo Trading в MT5 е изключен. Новите сигнали и "
                           "командите ще се отказват, докато не го оправиш."]
    bot.post(XAUUSD_SIGNAL, 505)
    assert broker.bot_positions() == [] and bot.sig(505)["status"] == "FAILED"
    assert any(n.startswith("❗ sc_n1001_505_0_0: rejected 10027") for n in bot.notes())
    bot.monitor()
    assert bot.notes() == []

    broker.algo_trading = True
    bot.monitor()
    assert bot.notes() == ["✅ MT5 отново може да търгува."]
    bot.edit(505, XAUUSD_SIGNAL.replace("4498.30", "4499.30"))  # refused by MT5 earlier: never resent by an edit
    assert broker.bot_positions() == []
    bot.post(AUDUSD_SIGNAL, 501)
    assert len(broker.bot_positions()) == 1
