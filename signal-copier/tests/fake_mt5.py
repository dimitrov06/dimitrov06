"""In-memory stand-in for the MetaTrader5 module (IC Markets-like symbols, hedging account).

Faithful to the real package where the bot's safety depends on it:
- calls never raise: on an IPC/terminal error they return None and last_error() says why
  (`fail = {"positions_get": 1}`: the next call(s) fail; `ipc_down = True`: every call fails, nothing executes);
- a position's ticket == identifier == its opening order's ticket, also when a pending order fills;
- deal times are trade-server time (`server_offset`, IC Markets = UTC+3 in October) and
  history_deals_get(date_from, date_to) filters on them; history_deals_get(position=) by position;
- REMOVE / SLTP / close of an order or position that is gone answer with a retcode, never an exception.
"""

from __future__ import annotations

import itertools
import time
from datetime import datetime
from types import SimpleNamespace as NS

INVALID_REQUEST = 10013


class FakeMt5:
    TRADE_ACTION_DEAL, TRADE_ACTION_PENDING, TRADE_ACTION_SLTP, TRADE_ACTION_REMOVE = 1, 5, 6, 8
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT, ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP = 2, 3, 4, 5
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    ORDER_TIME_GTC, ORDER_TIME_SPECIFIED = 0, 2
    ORDER_STATE_CANCELED, ORDER_STATE_FILLED, ORDER_STATE_REJECTED, ORDER_STATE_EXPIRED = 2, 4, 5, 6
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT = 0, 1
    ACCOUNT_MARGIN_MODE_RETAIL_NETTING, ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 0, 2

    def __init__(self, demo: bool = True, server_offset: int = 3 * 3600):
        self.demo = demo
        self.server_offset = server_offset
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
        self.history_orders: dict[int, NS] = {}
        self.deals: list[NS] = []
        self.sent: list[dict] = []
        # per order_send: "none" (not executed, returns None) | "lost" (executes, returns None) | "requote" |
        # "timeout" (10012, never executes) | "timeout_late" / "none_late" (10012 / None now, executes on settle())
        self.script: list[str] = []
        self.late: list[dict] = []  # requests the trade server is still working on
        self.fail: dict[str, int] = {}  # function name -> how many more calls return None (IPC timeout)
        self.ipc_down = False
        self.err = (1, "Success")
        # terminal / account switches (Algo Trading button, investor password, netting account ...)
        self.algo_trading, self.tradeapi_disabled, self.connected = True, False, True
        self.account_trade_allowed, self.trade_expert = True, True
        self.margin_mode = self.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
        self._ids = itertools.count(1000)

    # --- IPC ---
    def _down(self, name: str) -> bool:
        if self.ipc_down:
            self.err = (-10004, "No IPC connection")
            return True
        if self.fail.get(name):
            self.fail[name] -= 1
            self.err = (-10005, "IPC timeout")
            return True
        self.err = (1, "Success")
        return False

    def server_now(self) -> int:
        return int(time.time()) + self.server_offset

    def settle(self) -> None:
        """The trade server finishes the requests that were still in flight ("*_late" script modes)."""
        while self.late:
            self._execute(self.late.pop(0))

    # --- terminal ---
    def initialize(self, **kw):
        self.init_kw = kw
        return True

    def shutdown(self):
        return True

    def last_error(self):
        return self.err

    def terminal_info(self):
        if self._down("terminal_info"):
            return None
        return NS(connected=self.connected, trade_allowed=self.algo_trading, tradeapi_disabled=self.tradeapi_disabled)

    def account_info(self):
        if self._down("account_info"):
            return None
        return NS(login=5100123, server="ICMarketsSC-Demo", balance=self.balance, equity=self.equity,
                  currency="USD", trade_mode=0 if self.demo else 2, trade_allowed=self.account_trade_allowed,
                  trade_expert=self.trade_expert, margin_mode=self.margin_mode)

    def symbol_select(self, name, enable):
        return name in self.symbols

    def symbol_info(self, name):
        if self._down("symbol_info") or name not in self.symbols:
            return None
        d, vmin, step, tv, ts, fill = self.symbols[name]
        return NS(name=name, digits=d, volume_min=vmin, volume_step=step, volume_max=100.0,
                  trade_tick_value=tv, trade_tick_value_loss=tv, trade_tick_size=ts, filling_mode=fill)

    def symbol_info_tick(self, name):
        if self._down("symbol_info_tick"):
            return None
        bid, ask = self.ticks[name]
        return NS(bid=bid, ask=ask, time=self.server_now())

    # --- queries ---
    def positions_get(self, symbol=None, ticket=None):
        if self._down("positions_get"):
            return None
        return tuple(p for p in self.positions.values()
                     if (symbol is None or p.symbol == symbol) and (ticket is None or p.ticket == ticket))

    def orders_get(self, ticket=None):
        if self._down("orders_get"):
            return None
        return tuple(o for o in self.orders.values() if ticket is None or o.ticket == ticket)

    def history_orders_get(self, ticket=None):
        if self._down("history_orders_get"):
            return None
        return tuple(o for o in self.history_orders.values() if ticket is None or o.ticket == ticket)

    def history_deals_get(self, date_from=None, date_to=None, position=None):
        if self._down("history_deals_get"):
            return None
        out = [d for d in self.deals if position is None or d.position_id == position]
        if date_from is not None:  # the terminal compares the bounds with server-time deal stamps
            lo, hi = (x.timestamp() if isinstance(x, datetime) else float(x) for x in (date_from, date_to))
            out = [d for d in out if lo <= d.time <= hi]
        return tuple(out)

    # --- trading ---
    def order_send(self, req):
        self.sent.append(dict(req))
        if self._down("order_send"):
            return None  # never reached the terminal
        mode = self.script.pop(0) if self.script else None
        if mode == "none":
            return None
        if mode == "requote":
            return NS(retcode=10004, comment="Requote", order=0, deal=0, volume=0, price=0)
        if mode in ("timeout", "timeout_late"):
            if mode == "timeout_late":
                self.late.append(dict(req))
            return NS(retcode=10012, comment="Request timeout", order=0, deal=0, volume=0, price=0)
        if mode == "none_late":
            self.late.append(dict(req))
            self.err = (-10005, "IPC timeout")
            return None
        res = self._execute(req)
        if mode == "lost":
            self.err = (-10005, "IPC timeout")
            return None
        return res

    def _deal(self, position_id: int, order: int, entry: int, price: float, volume: float, profit: float,
              comment: str, magic: int) -> None:
        self.deals.append(NS(ticket=next(self._ids), order=order, position_id=position_id, entry=entry, price=price,
                             volume=volume, profit=profit, commission=-0.7, swap=0.0, fee=0.0,
                             time=self.server_now(), comment=comment, magic=magic))

    def _history_order(self, ticket: int, state: int, comment: str) -> None:
        self.history_orders[ticket] = NS(ticket=ticket, state=state, comment=comment)

    @staticmethod
    def _refused(comment="Invalid request"):
        return NS(retcode=INVALID_REQUEST, comment=comment, order=0, deal=0, volume=0, price=0)

    def _execute(self, req):
        a = req["action"]
        if not self.algo_trading or self.tradeapi_disabled:
            return NS(retcode=10027, comment="AutoTrading disabled by client", order=0, deal=0, volume=0, price=0)
        if a == self.TRADE_ACTION_DEAL and "position" in req:
            p = self.positions.get(req["position"])
            if p is None:
                return self._refused("Position doesn't exist")
            vol = round(req["volume"], 2)
            order = next(self._ids)
            self._history_order(order, self.ORDER_STATE_FILLED, req.get("comment", ""))
            self._deal(p.ticket, order, self.DEAL_ENTRY_OUT, req["price"], vol, 12.5 * vol / p.volume,
                       req.get("comment", ""), req.get("magic", 0))
            p.volume = round(p.volume - vol, 2)
            if p.volume <= 0:
                del self.positions[p.ticket]
            return NS(retcode=10009, comment="done", order=order, deal=0, volume=vol, price=req["price"])
        if a == self.TRADE_ACTION_DEAL:
            ticket = next(self._ids)
            buy = req["type"] == self.ORDER_TYPE_BUY
            self.positions[ticket] = NS(ticket=ticket, identifier=ticket, symbol=req["symbol"], type=0 if buy else 1,
                                        volume=req["volume"], price_open=req["price"], sl=req["sl"],
                                        tp=req["tp"], magic=req["magic"], comment=req["comment"])
            self._history_order(ticket, self.ORDER_STATE_FILLED, req["comment"])
            self._deal(ticket, ticket, self.DEAL_ENTRY_IN, req["price"], req["volume"], 0.0, req["comment"],
                       req["magic"])
            return NS(retcode=10009, comment="done", order=ticket, deal=self.deals[-1].ticket,
                      volume=req["volume"], price=req["price"])
        if a == self.TRADE_ACTION_PENDING:
            ticket = next(self._ids)
            self.orders[ticket] = NS(ticket=ticket, symbol=req["symbol"], type=req["type"],
                                     volume_current=req["volume"], price_open=req["price"], sl=req["sl"],
                                     tp=req["tp"], magic=req["magic"], comment=req["comment"])
            return NS(retcode=10008, comment="placed", order=ticket, deal=0, volume=req["volume"],
                      price=req["price"])
        if a == self.TRADE_ACTION_SLTP:
            p = self.positions.get(req["position"])
            if p is None:
                return self._refused("Position doesn't exist")
            p.sl, p.tp = req["sl"], req["tp"]
            return NS(retcode=10009, comment="done", order=0, deal=0, volume=0, price=0)
        if a == self.TRADE_ACTION_REMOVE:
            o = self.orders.pop(req["order"], None)
            if o is None:
                return self._refused()
            self._history_order(o.ticket, self.ORDER_STATE_CANCELED, o.comment)
            return NS(retcode=10009, comment="done", order=0, deal=0, volume=0, price=0)
        raise AssertionError(req)

    # --- broker-side events ---
    def fill_pending(self, ticket: int) -> int:
        """The pending order fills: the position keeps the order's ticket (hedging)."""
        o = self.orders.pop(ticket)
        self.positions[ticket] = NS(ticket=ticket, identifier=ticket, symbol=o.symbol,
                                    type=0 if o.type in (2, 4) else 1, volume=o.volume_current,
                                    price_open=o.price_open, sl=o.sl, tp=o.tp, magic=o.magic, comment=o.comment)
        self._history_order(ticket, self.ORDER_STATE_FILLED, o.comment)
        self._deal(ticket, ticket, self.DEAL_ENTRY_IN, o.price_open, o.volume_current, 0.0, o.comment, o.magic)
        return ticket

    def expire_pending(self, ticket: int) -> None:
        o = self.orders.pop(ticket)
        self._history_order(ticket, self.ORDER_STATE_EXPIRED, o.comment)

    def hit_tp(self, ticket: int, price: float) -> None:
        p = self.positions.pop(ticket)
        self._deal(ticket, next(self._ids), self.DEAL_ENTRY_OUT, price, p.volume, 45.0, "[tp]", p.magic)

    def market_sends(self) -> list[dict]:
        return [r for r in self.sent if r["action"] == self.TRADE_ACTION_DEAL and "position" not in r]


class StrictMt5(FakeMt5):
    """FakeMt5 plus the trade-server checks the stock fake skips (IC Markets: stops level 0):

    - SLTP: an SL beyond the closing price (BUY: SL >= Bid, SELL: SL <= Ask) or a TP on the wrong side -> 10016
      "Invalid stops"; the same SL and TP the position already has -> 10025 "No changes";
    - new market orders: SL/TP on the wrong side of the price -> 10016; pending price on the wrong side -> 10015.
    A refused request changes nothing.
    """

    @staticmethod
    def _no(code: int, comment: str):
        return NS(retcode=code, comment=comment, order=0, deal=0, volume=0, price=0)

    @staticmethod
    def _stops_ok(buy: bool, sl: float, tp: float, bid: float, ask: float) -> bool:
        return (not sl or (sl < bid if buy else sl > ask)) and (not tp or (tp > bid if buy else tp < ask))

    def order_send(self, req):
        a = req["action"]
        if a == self.TRADE_ACTION_SLTP and req.get("position") in self.positions:
            p = self.positions[req["position"]]
            bid, ask = self.ticks[p.symbol]
            if (req["sl"], req["tp"]) == (p.sl, p.tp):
                self.sent.append(dict(req))
                return self._no(10025, "No changes")
            if not self._stops_ok(p.type == self.POSITION_TYPE_BUY, req["sl"], req["tp"], bid, ask):
                self.sent.append(dict(req))
                return self._no(10016, "Invalid stops")
        elif a in (self.TRADE_ACTION_DEAL, self.TRADE_ACTION_PENDING) and "position" not in req:
            bid, ask = self.ticks[req["symbol"]]
            t = req["type"]
            if a == self.TRADE_ACTION_PENDING:
                ok = {self.ORDER_TYPE_BUY_LIMIT: req["price"] < ask, self.ORDER_TYPE_SELL_LIMIT: req["price"] > bid,
                      self.ORDER_TYPE_BUY_STOP: req["price"] > ask, self.ORDER_TYPE_SELL_STOP: req["price"] < bid}[t]
                if not ok:
                    self.sent.append(dict(req))
                    return self._no(10015, "Invalid price")
            elif not self._stops_ok(t == self.ORDER_TYPE_BUY, req["sl"], req["tp"], bid, ask):
                self.sent.append(dict(req))
                return self._no(10016, "Invalid stops")
        return super().order_send(req)

    def sltp_sends(self) -> list[dict]:
        return [r for r in self.sent if r["action"] == self.TRADE_ACTION_SLTP]
