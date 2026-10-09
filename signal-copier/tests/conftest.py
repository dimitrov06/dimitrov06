"""Shared fixtures: an in-memory fake of the OANDA v20 endpoints the bot uses."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from signal_copier.executor import Executor, OandaClient
from signal_copier.notifier import Notifier
from signal_copier.parser import SignalParser
from signal_copier.pipeline import Pipeline
from signal_copier.risk import RiskManager
from signal_copier.settings import Mode, OandaCredentials, Settings
from signal_copier.storage import Storage

NOW = datetime(2026, 10, 8, 14, 14, tzinfo=timezone.utc)

INSTRUMENTS = {
    "AUD_USD": {"name": "AUD_USD", "displayPrecision": 5, "tradeUnitsPrecision": 0, "minimumTradeSize": "1",
                "pipLocation": -4},
    "AUD_CAD": {"name": "AUD_CAD", "displayPrecision": 5, "tradeUnitsPrecision": 0, "minimumTradeSize": "1",
                "pipLocation": -4},
    "XAU_USD": {"name": "XAU_USD", "displayPrecision": 3, "tradeUnitsPrecision": 0, "minimumTradeSize": "1",
                "pipLocation": -2},
    "EUR_USD": {"name": "EUR_USD", "displayPrecision": 5, "tradeUnitsPrecision": 0, "minimumTradeSize": "1",
                "pipLocation": -4},
}


class FakeOanda:
    def __init__(self):
        self.balance = Decimal("10000")
        self.nav = Decimal("10000")
        self.prices = {"AUD_USD": ("0.69400", "0.69402"), "AUD_CAD": ("0.99540", "0.99543"),
                       "XAU_USD": ("4479.50", "4479.80"), "EUR_USD": ("1.10000", "1.10002")}
        self.home = {"USD": "1", "CAD": "0.73"}
        self.orders: dict[str, dict] = {}  # by client id
        self.trades: dict[str, dict] = {}
        self.next_id = 100
        self.requests: list[tuple[str, str, dict | None]] = []
        self.post_failures: list[str] = []  # "before" = never reaches OANDA, "after" = executed but response lost

    def _id(self) -> str:
        self.next_id += 1
        return str(self.next_id)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.replace("/v3/accounts/ACC", "")
        body = json.loads(req.content) if req.content else None
        self.requests.append((req.method, path, body))
        m = req.method

        if m == "GET" and path == "/summary":
            return httpx.Response(200, json={"account": {"balance": str(self.balance), "NAV": str(self.nav),
                                                         "currency": "USD"}})
        if m == "GET" and path == "/instruments":
            name = req.url.params["instruments"]
            return httpx.Response(200, json={"instruments": [INSTRUMENTS[name]] if name in INSTRUMENTS else []})
        if m == "GET" and path == "/pricing":
            name = req.url.params["instruments"]
            bid, ask = self.prices[name]
            return httpx.Response(200, json={
                "prices": [{"instrument": name, "bids": [{"price": bid}], "asks": [{"price": ask}]}],
                "homeConversions": [{"currency": c, "accountLoss": r} for c, r in self.home.items()],
            })
        if m == "POST" and path == "/orders":
            mode = self.post_failures.pop(0) if self.post_failures else None
            if mode == "before":
                raise httpx.ConnectTimeout("timeout", request=req)
            resp = self._create(body["order"])
            if mode == "after":
                raise httpx.ReadTimeout("response lost", request=req)
            return resp
        if m == "GET" and path.startswith("/orders/@"):
            o = self.orders.get(path.split("@", 1)[1])
            return httpx.Response(200, json={"order": o}) if o else httpx.Response(404, json={"errorMessage": "nf"})
        if m == "PUT" and path.startswith("/orders/@") and path.endswith("/cancel"):
            o = self.orders.get(path.split("@", 1)[1].removesuffix("/cancel"))
            if not o:
                return httpx.Response(404, json={})
            o["state"] = "CANCELLED"
            return httpx.Response(200, json={"orderCancelTransaction": {"orderID": o["id"]}})
        if m == "GET" and path == "/openTrades":
            return httpx.Response(200, json={"trades": [t for t in self.trades.values() if t["state"] == "OPEN"]})
        if path.startswith("/trades/"):
            parts = path.split("/")
            t = self.trades.get(parts[2])
            if t is None:
                return httpx.Response(404, json={})
            if m == "GET" and len(parts) == 3:
                return httpx.Response(200, json={"trade": t})
            if m == "PUT" and parts[3] == "orders":
                if "stopLoss" in body:
                    t["stopLossOrder"] = {"price": body["stopLoss"]["price"]}
                if "takeProfit" in body:
                    t["takeProfitOrder"] = {"price": body["takeProfit"]["price"]}
                return httpx.Response(200, json={})
            if m == "PUT" and parts[3] == "close":
                if body["units"] == "ALL":
                    self.close(t["id"], "0.69530", "12.50")
                else:
                    cur = Decimal(t["currentUnits"])
                    sign = 1 if cur > 0 else -1
                    t["currentUnits"] = str(cur - sign * Decimal(body["units"]))
                return httpx.Response(200, json={"orderFillTransaction": {}})
        return httpx.Response(404, json={"errorMessage": f"unhandled {m} {path}"})

    def _create(self, o: dict) -> httpx.Response:
        cid = o["clientExtensions"]["id"]
        if cid in self.orders:
            return httpx.Response(400, json={"errorCode": "CLIENT_ORDER_ID_ALREADY_EXISTS"})
        oid = self._id()
        order = {"id": oid, "state": "PENDING", "clientExtensions": o["clientExtensions"], **o}
        self.orders[cid] = order
        create = {"id": oid, "type": f"{o['type']}_ORDER", "price": o.get("price")}
        if o["type"] != "MARKET":
            return httpx.Response(201, json={"orderCreateTransaction": create})
        bid, ask = self.prices[o["instrument"]]
        price = ask if Decimal(o["units"]) > 0 else bid
        tid = self._id()
        self.trades[tid] = {"id": tid, "instrument": o["instrument"], "initialUnits": o["units"],
                            "currentUnits": o["units"], "price": price, "openTime": "2026-10-08T14:14:05Z",
                            "state": "OPEN", "clientExtensions": o["tradeClientExtensions"],
                            "stopLossOrder": {"price": o["stopLossOnFill"]["price"]},
                            "takeProfitOrder": {"price": (o.get("takeProfitOnFill") or {}).get("price")}}
        order.update(state="FILLED", tradeOpenedID=tid)
        return httpx.Response(201, json={
            "orderCreateTransaction": create,
            "orderFillTransaction": {"id": self._id(), "price": price, "time": "2026-10-08T14:14:05Z",
                                     "tradeOpened": {"tradeID": tid, "units": o["units"], "price": price}},
        })

    def fill_pending(self, cid: str) -> str:
        o = self.orders[cid]
        tid = self._id()
        self.trades[tid] = {"id": tid, "instrument": o["instrument"], "initialUnits": o["units"],
                            "currentUnits": o["units"], "price": o["price"], "openTime": "2026-10-08T15:00:00Z",
                            "state": "OPEN", "clientExtensions": o["tradeClientExtensions"]}
        o.update(state="FILLED", tradeOpenedID=tid)
        return tid

    def close(self, trade_id: str, price: str, pl: str) -> None:
        t = self.trades[trade_id]
        t.update(state="CLOSED", averageClosePrice=price, realizedPL=pl, closeTime="2026-10-08T17:04:00Z",
                 currentUnits="0")

    def market_orders(self) -> list[dict]:
        return [r for r in self.requests if r[0] == "POST"]


@pytest.fixture
def fake():
    return FakeOanda()


async def _no_sleep(_):
    return None


@pytest.fixture
def make_pipeline(fake):
    created = []

    def _make(mode: Mode = Mode.PAPER, clock_offset_s: int = 5, **risk_overrides):
        settings = Settings.model_validate({"mode": mode.value, "risk": risk_overrides})
        storage = Storage(":memory:")
        notifier = Notifier(None, None)
        client = executor = None
        if mode is not Mode.RECORD:
            creds = OandaCredentials(base_url="https://fake", token="t", account_id="ACC")
            client = OandaClient(creds, retry_attempts=3, retry_base_delay_s=0, transport=fake.transport(),
                                 sleep=_no_sleep)
            executor = Executor(client, storage, retry_attempts=3, retry_base_delay_s=0)
        p = Pipeline(settings, storage, SignalParser(), notifier, RiskManager(settings.risk), client, executor,
                     clock=lambda: NOW + timedelta(seconds=clock_offset_s))
        created.append(p)
        return p

    return _make
