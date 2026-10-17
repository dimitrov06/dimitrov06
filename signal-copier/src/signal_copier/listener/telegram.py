"""Telethon userbot: reads the signal channel(s) without admin rights.

Restart safety: Telethon does not replay missed updates (catch_up=False), so at
start the messages posted since the last stored one are fetched from history and
handled in order with their real dates (old signals fail the age check, closes and
cancels are applied); a gap that is too long or too old is only reported. Every
message is deduplicated in SQLite, and edits of messages older than the start
watermark that the bot never saw are ignored (they could resurrect old signals).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from signal_copier import log
from signal_copier.models import IncomingMessage
from signal_copier.settings import ConfigError
from signal_copier.storage import Storage

logger = log.get("listener")

OnMessage = Callable[[IncomingMessage], Awaitable[None]]
Notify = Callable[[str], Awaitable[None]]
CATCH_UP_MAX_MESSAGES = 200
CATCH_UP_MAX_AGE = timedelta(hours=48)


def make_client(api_id: int, api_hash: str, session_dir: str, session_name: str):
    from telethon import TelegramClient  # imported lazily: tests and record tooling don't need it

    Path(session_dir).mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(Path(session_dir) / session_name), api_id, api_hash, catch_up=False)


def _norm(title: str) -> str:
    return " ".join("".join(c for c in title if c.isalnum() or c.isspace()).casefold().split())


def match_title(wanted: str, titles: list[tuple[object, str]]) -> object:
    """Pick the one dialog whose title matches: exact (ignoring case/emojis) first, then one that contains it.

    A title that is only a piece of the wanted name ("Trading Signals", a contact "Al") never matches.
    """
    w = _norm(wanted)
    exact = [e for e, t in titles if _norm(t) == w]
    if len(exact) == 1:
        return exact[0]
    partial = [(e, t) for e, t in titles if w and w in _norm(t)]
    if len(exact) > 1 or len(partial) > 1:
        names = ", ".join(t for _, t in partial) or wanted
        raise ValueError(f"channel '{wanted}' is ambiguous: {names}. Use its numeric id (-100...) in config.yaml")
    if not partial:
        raise ValueError(f"no chat named '{wanted}' among your Telegram chats (are you a member?)")
    return partial[0][0]


async def resolve_channel(client, ch: str | int, storage: Storage | None = None):
    """@username, t.me link, numeric id (-100...) or the group's name as shown in Telegram.

    A name is matched once; its chat id is then kept in `storage`, so a renamed group is still the same chat
    and another chat whose title happens to match is never picked up.
    """
    if isinstance(ch, int) or str(ch).lstrip("-").isdigit():
        return await client.get_entity(int(ch))
    s = str(ch).strip()
    if s.startswith("@") or "t.me/" in s:
        return await client.get_entity(s)
    from telethon import utils

    dialogs = await client.get_dialogs()
    key = f"channel_peer:{_norm(s)}"
    known = storage.get_state(key) if storage is not None else None
    if known is not None:
        for d in dialogs:
            if str(utils.get_peer_id(d.entity)) == known:
                return d.entity
        raise ConfigError(f"chat '{s}' (id {known}) is no longer among your Telegram chats (removed from the group?)")
    entity = match_title(s, [(d.entity, d.name or "") for d in dialogs])
    if storage is not None:
        storage.set_state(key, str(utils.get_peer_id(entity)))
    return entity


def to_incoming(chat_id: int, m, edited: bool) -> IncomingMessage:
    reply_to = getattr(m.reply_to, "reply_to_msg_id", None) if m.reply_to else None
    return IncomingMessage(
        chat_id=chat_id,
        message_id=m.id,
        text=m.raw_text or "",
        # always the post date: an edit hours later must not make an old signal look fresh to the age check
        date=m.date,
        reply_to_msg_id=reply_to,
        edited=edited,
    )


class TelegramListener:
    def __init__(self, client, channels: list[str | int], storage: Storage, on_message: OnMessage,
                 notify: Notify | None = None, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.client = client
        self.channels = channels
        self.storage = storage
        self.on_message = on_message
        self.notify = notify
        self.clock = clock
        self.watermarks: dict[int, int] = {}

    async def _say(self, text: str) -> None:
        if self.notify is not None:
            await self.notify(text)

    async def run(self, stop: asyncio.Event) -> None:
        from telethon import events, utils

        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise ConfigError("Telegram session is not logged in. Run: python -m signal_copier login")

        chat_ids, missed, names = [], [], []
        for ch in self.channels:
            entity = await resolve_channel(self.client, ch, self.storage)
            chat_id = utils.get_peer_id(entity)
            chat_ids.append(chat_id)
            title = getattr(entity, "title", None) or str(ch)
            names.append(f"{title} ({chat_id})")
            by_name = not (isinstance(ch, int) or str(ch).lstrip("-").isdigit() or str(ch).strip().startswith("@")
                           or "t.me/" in str(ch))
            if by_name and _norm(str(ch)) not in _norm(title):
                await self._say(f"ℹ️ Групата '{ch}' вече се казва '{title}'. Следя същия чат (id {chat_id}).")
            latest = await self.client.get_messages(entity, limit=1)
            last = self.storage.last_message_id(chat_id)
            latest_id = latest[0].id if latest else 0
            self.watermarks[chat_id] = max(latest_id, last or 0)
            if last and latest_id > last:
                missed.append((chat_id, entity, last))
            logger.info("listening", channel=str(ch), chat_id=chat_id, watermark=self.watermarks[chat_id])
        await self._say("👂 Следя: " + ", ".join(names))

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

        for chat_id, entity, last in missed:  # handlers are on: anything newer arrives live meanwhile
            await self._catch_up(chat_id, entity, last)

        disconnected = asyncio.ensure_future(self.client.run_until_disconnected())
        stopped = asyncio.ensure_future(stop.wait())
        await asyncio.wait({disconnected, stopped}, return_when=asyncio.FIRST_COMPLETED)
        await self.client.disconnect()

    async def _catch_up(self, chat_id: int, entity, last: int) -> None:
        """Messages posted while the bot was down (after the last stored one, up to the start watermark)."""
        msgs = await self.client.get_messages(entity, limit=CATCH_UP_MAX_MESSAGES + 1, min_id=last, reverse=True)
        msgs = sorted((m for m in msgs if last < m.id <= self.watermarks[chat_id]), key=lambda m: m.id)
        if not msgs:
            return
        first = msgs[0].date
        when = f"от {first:%d.%m %H:%M} UTC"
        if len(msgs) > CATCH_UP_MAX_MESSAGES or self.clock() - first > CATCH_UP_MAX_AGE:
            logger.warning("catch_up_skipped", chat_id=chat_id, messages=len(msgs))
            await self._say(f"⚠️ Докато ботът беше спрян, в канала има {len(msgs)}"
                            f"{'+' if len(msgs) > CATCH_UP_MAX_MESSAGES else ''} съобщения ({when}). Твърде много/"
                            "стари са и НЕ ги обработвам автоматично: провери отворените сделки и поръчки в MT5.")
            return
        logger.info("catch_up", chat_id=chat_id, messages=len(msgs))
        await self._say(f"⚠️ Докато ботът беше спрян, в канала има {len(msgs)} съобщения ({when}). Обработвам ги "
                        "сега: старите сигнали се отхвърлят, командите (close, cancel, BE) се изпълняват.")
        for m in msgs:
            await self.on_message(to_incoming(chat_id, m, edited=False))

    def _seen(self, chat_id: int, message_id: int) -> bool:
        row = self.storage.conn.execute(
            "SELECT 1 FROM messages WHERE chat_id=? AND message_id=? LIMIT 1", (chat_id, message_id)
        ).fetchone()
        return row is not None
