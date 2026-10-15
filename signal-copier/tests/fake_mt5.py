"""In-memory stand-in for the MetaTrader5 module (IC Markets-like symbols, hedging account)."""

from __future__ import annotations

import itertools
import time
from types import SimpleNamespace as NS


class FakeMt5:
    TRADE_ACTION_DEAL, TRADE_ACTION_PENDING, TRADE_ACTION_SLTP, TRADE_ACTION_REMOVE = 1, 5, 6, 8
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT, ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP = 2, 3, 4, 5
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    ORDER_TIME_GTC, ORDER_TIME_SPECIFIED = 0, 2
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT = 0, 1

    def __init__(self, demo: bool = True):
        self.demo = demo
        self.balance, self.equity = 10000.0, 10000.0
        self.symbols = {
            # name: (digits, volume_min, volume_step, tick_value, tick_size, filling_mode)
            "AUDUSD": (5, 0.01, 0.01, 1.0, 0.00001, 2),  # 1 lot = 100k AUD, 1 pip(0.0001) = 10 USD
            "XAUUSD": (2, 0.01, 0.01, 1.0, 0.01, 2),  # 1 lot = 100 oz, 0.01 move = 1 USD
            "US30": (2, 0.1, 0.1, 0.01, 0.01, 2),
        }
        self.ticks = {"AUDUSD": (0.69400, 0.69402), "XAUUSD": (4479.50, 4479.80), "US30": (42000.0, 42002.0)}
        self.positions: dict[int, NS] = {}
        self.orders: dict[int, NS] = {}
        self.deals: list[NS] = []
        self.sent: list[dict] = []
        self.script: list[str] = []  # per order_send: "none" | "lost" (executes, returns None) | "requote" | "timeout"
        self._ids = itertools.count(1000)

    # --- terminal ---
    def initialize(self, **kw):
        self.init_kw = kw
        return True

    def shutdown(self):
        return True

    def last_error(self):
        return (1, "fake")

    def account_info(self):
        return NS(login=5100123, server="ICMarketsSC-Demo", balance=self.balance, equity=self.equity,
                  currency="USD", trade_mode=0 if self.demo else 2)

    def symbol_select(self, name, enable):
        return name in self.symbols

    def symbol_info(self, name):
        if name not in self.symbols:
            return None
        d, vmin, step, tv, ts, fill = self.symbols[name]
        return NS(name=name, digits=d, volume_min=vmin, volume_step=step, volume_max=100.0,
                  trade_tick_value=tv, trade_tick_value_loss=tv, trade_tick_size=ts, filling_mode=fill)

    def symbol_info_tick(self, name):
        bid, ask = self.ticks[name]
        return NS(bid=bid, ask=ask, time=int(time.time()))

    # --- queries ---
    def positions_get(self, symbol=None, ticket=None):
        out = [p for p in self.positions.values()
               if (symbol is None or p.symbol == symbol) and (ticket is None or p.ticket == ticket)]
        return tuple(out)

    def orders_get(self, ticket=None):
        return tuple(o for o in self.orders.values() if ticket is None or o.ticket == ticket)

    def history_deals_get(self, *args, position=None):
        return tuple(d for d in self.deals if position is None or d.position_id == position)

    # --- trading ---
    def order_send(self, req):
        self.sent.append(dict(req))
        mode = self.script.pop(0) if self.script else None
        if mode == "none":
            return None
        if mode == "requote":
            return NS(retcode=10004, comment="Requote", order=0, deal=0, volume=0, price=0)
        if mode == "timeout":
            return NS(retcode=10012, comment="Timeout", order=0, deal=0, volume=0, price=0)
        res = self._execute(req)
        return None if mode == "lost" else res

    def _execute(self, req):
        a = req["action"]
        if a == self.TRADE_ACTION_DEAL and "position" in req:
            p = self.positions[req["position"]]
            vol = round(req["volume"], 2)
            self.deals.append(NS(ticket=next(self._ids), position_id=p.ticket, entry=self.DEAL_ENTRY_OUT,
                                 price=req["price"], volume=vol, profit=12.5 * vol / p.volume, commission=-0.7,
                                 swap=0.0, fee=0.0, time=int(time.time()), comment=req.get("comment", "")))
            p.volume = round(p.volume - vol, 2)
            if p.volume <= 0:
                del self.positions[p.ticket]
            return NS(retcode=10009, comment="done", order=next(self._ids), deal=0, volume=vol, price=req["price"])
        if a == self.TRADE_ACTION_DEAL:
            ticket = next(self._ids)
            buy = req["type"] == self.ORDER_TYPE_BUY
            self.positions[ticket] = NS(ticket=ticket, symbol=req["symbol"], type=0 if buy else 1,
                                        volume=req["volume"], price_open=req["price"], sl=req["sl"],
                                        tp=req["tp"], magic=req["magic"], comment=req["comment"])
            self.deals.append(NS(ticket=next(self._ids), position_id=ticket, entry=self.DEAL_ENTRY_IN,
                                 price=req["price"], volume=req["volume"], profit=0.0, commission=-0.7, swap=0.0,
                                 fee=0.0, time=int(time.time()), comment=req["comment"]))
            return NS(retcode=10009, comment="done", order=ticket, deal=ticket + 1, volume=req["volume"],
                      price=req["price"])
        if a == self.TRADE_ACTION_PENDING:
            ticket = next(self._ids)
            self.orders[ticket] = NS(ticket=ticket, symbol=req["symbol"], type=req["type"],
                                     volume_current=req["volume"], price_open=req["price"], sl=req["sl"],
                                     tp=req["tp"], magic=req["magic"], comment=req["comment"])
            return NS(retcode=10008, comment="placed", order=ticket, deal=0, volume=req["volume"],
                      price=req["price"])
        if a == self.TRADE_ACTION_SLTP:
            p = self.positions[req["position"]]
            p.sl, p.tp = req["sl"], req["tp"]
            return NS(retcode=10009, comment="done", order=0, deal=0, volume=0, price=0)
        if a == self.TRADE_ACTION_REMOVE:
            self.orders.pop(req["order"])
            return NS(retcode=10009, comment="done", order=0, deal=0, volume=0, price=0)
        raise AssertionError(req)

    def fill_pending(self, ticket: int) -> int:
        o = self.orders.pop(ticket)
        pt = next(self._ids)
        self.positions[pt] = NS(ticket=pt, symbol=o.symbol, type=0 if o.type in (2, 4) else 1,
                                volume=o.volume_current, price_open=o.price_open, sl=o.sl, tp=o.tp, magic=o.magic,
                                comment=o.comment)
        return pt

    def hit_tp(self, ticket: int, price: float) -> None:
        p = self.positions.pop(ticket)
        self.deals.append(NS(ticket=next(self._ids), position_id=ticket, entry=self.DEAL_ENTRY_OUT, price=price,
                             volume=p.volume, profit=45.0, commission=-0.7, swap=0.0, fee=0.0,
                             time=int(time.time()), comment="[tp]"))

    def market_sends(self) -> list[dict]:
        return [r for r in self.sent if r["action"] == self.TRADE_ACTION_DEAL and "position" not in r]
