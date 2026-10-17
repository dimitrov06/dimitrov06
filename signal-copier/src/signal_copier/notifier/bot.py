"""Your own Telegram bot (Bot API): notifications + /stop /resume /status."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

from signal_copier import log

logger = log.get("notifier")

CommandHandler = Callable[[str], Awaitable[str]]
COMMANDS = ("/stop", "/resume", "/status", "/help", "/start", "/commands")
SEND_TIMEOUT_S = 10  # one sendMessage; getUpdates long-polls 30 s on the client's longer timeout
DRAIN_TIMEOUT_S = 15  # aclose() waits this long for queued messages


class Notifier:
    """Sends to one chat (yours). Never raises: a failed notification must not stop trading logic.

    send() only queues the text: a slow or hanging Bot API never delays a trade, a close or the age check.
    """

    def __init__(self, token: str | None, chat_id: int | None, transport: httpx.AsyncBaseTransport | None = None):
        self.enabled = bool(token and chat_id)
        self.chat_id = chat_id
        self.http = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}/" if token else "http://disabled/",
            timeout=40,
            transport=transport,
        )
        self.sent: list[str] = []  # kept for tests / debugging
        self.failed = 0  # messages the Bot API did not take (network down, wrong token)
        self._queue: asyncio.Queue[str] | None = None
        self._worker: asyncio.Task | None = None

    async def aclose(self) -> None:
        if self._queue is not None and self._worker is not None and not self._worker.done():
            try:
                await asyncio.wait_for(self._queue.join(), DRAIN_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.error("notify_dropped", queued=self._queue.qsize())
            self._worker.cancel()
        await self.http.aclose()

    async def send(self, text: str) -> None:
        self.sent.append(text)
        logger.info("notify", text=text)
        if not self.enabled:
            return
        if self._worker is None or self._worker.done():
            self._queue = asyncio.Queue()
            self._worker = asyncio.get_running_loop().create_task(self._run_queue(), name="notifier")
        self._queue.put_nowait(text)

    async def _run_queue(self) -> None:
        while True:
            text = await self._queue.get()
            try:
                await self._post(text)
            finally:
                self._queue.task_done()

    async def _post(self, text: str) -> None:
        try:
            r = await self.http.post("sendMessage", json={"chat_id": self.chat_id, "text": text[:4000],
                                                          "disable_web_page_preview": True},
                                     timeout=SEND_TIMEOUT_S)
            if r.status_code != 200:
                self.failed += 1
                logger.error("notify_failed", status=r.status_code, body=r.text[:300])
        except Exception as e:  # never raises: httpx errors, timeouts, anything
            self.failed += 1
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
                        self.sent.append(reply)
                        await self._post(reply)  # the owner is waiting for this answer: sent directly
            except (httpx.HTTPError, ValueError) as e:
                logger.warning("poll_failed", error=repr(e))
                await asyncio.sleep(5)
