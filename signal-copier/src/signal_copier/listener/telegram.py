"""Telethon userbot: reads the signal channel(s) without admin rights.

Restart safety: Telethon does not replay missed updates (catch_up=False), every
message is deduplicated in SQLite, and edits of messages older than the start
watermark that the bot never saw are ignored (they could resurrect old signals).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from signal_copier import log
from signal_copier.models import IncomingMessage
from signal_copier.storage import Storage

logger = log.get("listener")

OnMessage = Callable[[IncomingMessage], Awaitable[None]]


def make_client(api_id: int, api_hash: str, session_dir: str, session_name: str):
    from telethon import TelegramClient  # imported lazily: tests and record tooling don't need it

    Path(session_dir).mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(Path(session_dir) / session_name), api_id, api_hash, catch_up=False)


def to_incoming(chat_id: int, m, edited: bool) -> IncomingMessage:
    reply_to = getattr(m.reply_to, "reply_to_msg_id", None) if m.reply_to else None
    return IncomingMessage(
        chat_id=chat_id,
        message_id=m.id,
        text=m.raw_text or "",
        # for an edit the content became valid at edit time; risk/ age check uses this
        date=(m.edit_date or m.date) if edited else m.date,
        reply_to_msg_id=reply_to,
        edited=edited,
    )


class TelegramListener:
    def __init__(self, client, channels: list[str | int], storage: Storage, on_message: OnMessage):
        self.client = client
        self.channels = channels
        self.storage = storage
        self.on_message = on_message
        self.watermarks: dict[int, int] = {}

    async def run(self, stop: asyncio.Event) -> None:
        from telethon import events, utils

        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Telegram session is not logged in. Run: python -m signal_copier login")

        chat_ids = []
        for ch in self.channels:
            entity = await self.client.get_entity(ch)
            chat_id = utils.get_peer_id(entity)
            chat_ids.append(chat_id)
            latest = await self.client.get_messages(entity, limit=1)
            self.watermarks[chat_id] = max(latest[0].id if latest else 0, self.storage.last_message_id(chat_id) or 0)
            logger.info("listening", channel=str(ch), chat_id=chat_id, watermark=self.watermarks[chat_id])

        @self.client.on(events.NewMessage(chats=chat_ids))
        async def on_new(event):
            if event.message.id <= self.watermarks.get(event.chat_id, 0):
                return  # already existed before start
            await self.on_message(to_incoming(event.chat_id, event.message, edited=False))

        @self.client.on(events.MessageEdited(chats=chat_ids))
        async def on_edit(event):
            m = event.message
            if m.id <= self.watermarks.get(event.chat_id, 0) and not self._seen(event.chat_id, m.id):
                logger.info("ignored_old_edit", chat_id=event.chat_id, message_id=m.id)
                return
            await self.on_message(to_incoming(event.chat_id, m, edited=True))

        disconnected = asyncio.ensure_future(self.client.run_until_disconnected())
        stopped = asyncio.ensure_future(stop.wait())
        await asyncio.wait({disconnected, stopped}, return_when=asyncio.FIRST_COMPLETED)
        await self.client.disconnect()

    def _seen(self, chat_id: int, message_id: int) -> bool:
        row = self.storage.conn.execute(
            "SELECT 1 FROM messages WHERE chat_id=? AND message_id=? LIMIT 1", (chat_id, message_id)
        ).fetchone()
        return row is not None
