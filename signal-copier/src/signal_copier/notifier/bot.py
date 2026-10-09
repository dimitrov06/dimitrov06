"""Your own Telegram bot (Bot API): notifications + /stop /resume /status."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

from signal_copier import log

logger = log.get("notifier")

CommandHandler = Callable[[str], Awaitable[str]]
COMMANDS = ("/stop", "/resume", "/status", "/help")


class Notifier:
    """Sends to one chat (yours). Never raises: a failed notification must not stop trading logic."""

    def __init__(self, token: str | None, chat_id: int | None, transport: httpx.AsyncBaseTransport | None = None):
        self.enabled = bool(token and chat_id)
        self.chat_id = chat_id
        self.http = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}/" if token else "http://disabled/",
            timeout=40,
            transport=transport,
        )
        self.sent: list[str] = []  # kept for tests / debugging

    async def aclose(self) -> None:
        await self.http.aclose()

    async def send(self, text: str) -> None:
        self.sent.append(text)
        logger.info("notify", text=text)
        if not self.enabled:
            return
        try:
            r = await self.http.post("sendMessage", json={"chat_id": self.chat_id, "text": text[:4000],
                                                          "disable_web_page_preview": True})
            if r.status_code != 200:
                logger.error("notify_failed", status=r.status_code, body=r.text[:300])
        except httpx.HTTPError as e:
            logger.error("notify_failed", error=repr(e))

    async def poll_commands(self, handler: CommandHandler, stop: asyncio.Event) -> None:
        """Long-poll getUpdates. Only messages from NOTIFY_CHAT_ID are obeyed."""
        if not self.enabled:
            return
        offset = None
        while not stop.is_set():
            try:
                params = {"timeout": 30, "allowed_updates": '["message"]'}
                if offset is not None:
                    params["offset"] = offset
                r = await self.http.get("getUpdates", params=params)
                for upd in r.json().get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    text = (msg.get("text") or "").strip().split("@")[0].split(" ")[0].lower()
                    if msg.get("chat", {}).get("id") != self.chat_id:
                        logger.warning("command_from_unknown_chat", chat=msg.get("chat", {}).get("id"))
                        continue
                    if text in COMMANDS:
                        reply = await handler(text)
                        await self.send(reply)
            except (httpx.HTTPError, ValueError) as e:
                logger.warning("poll_failed", error=repr(e))
                await asyncio.sleep(5)
