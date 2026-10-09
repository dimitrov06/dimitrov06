"""Thin async OANDA v20 REST client."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx

from signal_copier import log
from signal_copier.risk import InstrumentInfo
from signal_copier.settings import OandaCredentials

logger = log.get("oanda")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class OandaError(RuntimeError):
    def __init__(self, status: int, body: Any):
        super().__init__(f"OANDA {status}: {body}")
        self.status = status
        self.body = body if isinstance(body, dict) else {"raw": body}

    @property
    def error_code(self) -> str | None:
        return self.body.get("errorCode") or (self.body.get("orderRejectTransaction") or {}).get("rejectReason")


class TransientError(RuntimeError):
    """Network problem or 5xx: the request may or may not have reached OANDA."""


class OandaClient:
    def __init__(
        self,
        creds: OandaCredentials,
        timeout_s: float = 10.0,
        retry_attempts: int = 4,
        retry_base_delay_s: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep=asyncio.sleep,
    ):
        self.account_id = creds.account_id
        self.retry_attempts = retry_attempts
        self.retry_base_delay_s = retry_base_delay_s
        self.sleep = sleep
        self.http = httpx.AsyncClient(
            base_url=creds.base_url,
            timeout=timeout_s,
            transport=transport,
            headers={
                "Authorization": f"Bearer {creds.token}",
                "Content-Type": "application/json",
                "Accept-Datetime-Format": "RFC3339",
            },
        )
        self._instruments: dict[str, InstrumentInfo] = {}

    async def aclose(self) -> None:
        await self.http.aclose()

    @property
    def _acct(self) -> str:
        return f"/v3/accounts/{self.account_id}"

    async def _once(self, method: str, path: str, **kw) -> dict:
        try:
            r = await self.http.request(method, path, **kw)
        except httpx.TransportError as e:
            raise TransientError(f"{method} {path}: {e!r}") from e
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text}
        if r.status_code in RETRYABLE_STATUS:
            raise TransientError(f"{method} {path}: HTTP {r.status_code} {body}")
        if r.status_code >= 400:
            raise OandaError(r.status_code, body)
        return body

    async def _retrying(self, method: str, path: str, **kw) -> dict:
        """Only for requests that are safe to repeat (GET, setting SL/TP, full close, cancel)."""
        for attempt in range(self.retry_attempts):
            try:
                return await self._once(method, path, **kw)
            except TransientError as e:
                if attempt == self.retry_attempts - 1:
                    raise
                delay = self.retry_base_delay_s * 2**attempt
                logger.warning("oanda_retry", error=str(e), attempt=attempt + 1, delay_s=delay)
                await self.sleep(delay)
        raise AssertionError("unreachable")

    # ----------------------------------------------------------------- reads

    async def account_summary(self) -> dict:
        return (await self._retrying("GET", f"{self._acct}/summary"))["account"]

    async def instrument(self, name: str) -> InstrumentInfo:
        if name not in self._instruments:
            body = await self._retrying("GET", f"{self._acct}/instruments", params={"instruments": name})
            if not body.get("instruments"):
                raise OandaError(404, {"errorMessage": f"instrument {name} not tradeable on this account"})
            i = body["instruments"][0]
            self._instruments[name] = InstrumentInfo(
                name=i["name"],
                display_precision=int(i["displayPrecision"]),
                trade_units_precision=int(i["tradeUnitsPrecision"]),
                minimum_trade_size=Decimal(i["minimumTradeSize"]),
                pip_location=int(i["pipLocation"]),
            )
        return self._instruments[name]

    async def pricing(self, instrument: str) -> tuple[Decimal, Decimal, Decimal]:
        """(bid, ask, quote_to_home) for the instrument."""
        body = await self._retrying(
            "GET", f"{self._acct}/pricing", params={"instruments": instrument, "includeHomeConversions": "true"}
        )
        p = body["prices"][0]
        bid, ask = Decimal(p["bids"][0]["price"]), Decimal(p["asks"][0]["price"])
        quote = instrument.split("_")[1]
        rate = Decimal(1)
        for c in body.get("homeConversions", []):
            if c["currency"] == quote:
                rate = Decimal(c.get("accountLoss") or c.get("positionValue") or "1")
        return bid, ask, rate

    async def open_trades(self) -> list[dict]:
        return (await self._retrying("GET", f"{self._acct}/openTrades"))["trades"]

    async def get_trade(self, trade_id: str) -> dict:
        return (await self._retrying("GET", f"{self._acct}/trades/{trade_id}"))["trade"]

    async def order_by_client_id(self, client_id: str) -> dict | None:
        try:
            return (await self._retrying("GET", f"{self._acct}/orders/@{client_id}"))["order"]
        except OandaError as e:
            if e.status == 404:
                return None
            raise

    # ---------------------------------------------------------------- writes

    async def create_order_once(self, order: dict) -> dict:
        """POST an order exactly once. Never retried here: the executor looks the order up before resending."""
        return await self._once("POST", f"{self._acct}/orders", json={"order": order})

    async def set_trade_orders(self, trade_id: str, sl: str | None = None, tp: str | None = None) -> dict:
        body: dict[str, Any] = {}
        if sl is not None:
            body["stopLoss"] = {"price": sl, "timeInForce": "GTC"}
        if tp is not None:
            body["takeProfit"] = {"price": tp, "timeInForce": "GTC"}
        return await self._retrying("PUT", f"{self._acct}/trades/{trade_id}/orders", json=body)

    async def close_trade(self, trade_id: str, units: str = "ALL") -> dict:
        if units == "ALL":
            return await self._retrying("PUT", f"{self._acct}/trades/{trade_id}/close", json={"units": "ALL"})
        # partial close is not idempotent: one attempt only
        return await self._once("PUT", f"{self._acct}/trades/{trade_id}/close", json={"units": units})

    async def cancel_order(self, order_specifier: str) -> dict:
        return await self._retrying("PUT", f"{self._acct}/orders/{order_specifier}/cancel")
