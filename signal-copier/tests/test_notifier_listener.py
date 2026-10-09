import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx

from conftest import NOW
from signal_copier.listener.telegram import to_incoming
from signal_copier.notifier import Notifier


def test_commands_only_from_owner_chat():
    sent, calls = [], {"n": 0}
    stop = asyncio.Event()

    def handle(req: httpx.Request):
        if req.url.path.endswith("getUpdates"):
            calls["n"] += 1
            if calls["n"] > 1:
                stop.set()
                return httpx.Response(200, json={"ok": True, "result": []})
            return httpx.Response(200, json={"ok": True, "result": [
                {"update_id": 1, "message": {"chat": {"id": 999}, "text": "/stop"}},  # stranger
                {"update_id": 2, "message": {"chat": {"id": 42}, "text": "/status@my_bot"}},
            ]})
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})

    n = Notifier("TOKEN", 42, transport=httpx.MockTransport(handle))
    received = []

    async def handler(cmd):
        received.append(cmd)
        return "ok"

    asyncio.run(n.poll_commands(handler, stop))
    assert received == ["/status"]
    assert sent == [{"chat_id": 42, "text": "ok", "disable_web_page_preview": True}]


def test_notifier_never_raises_on_network_error():
    def boom(req):
        raise httpx.ConnectError("down", request=req)

    n = Notifier("TOKEN", 42, transport=httpx.MockTransport(boom))
    asyncio.run(n.send("hello"))  # no exception
    assert n.sent == ["hello"]


def test_to_incoming_uses_edit_date_for_edits():
    m = SimpleNamespace(id=7, raw_text="XAUUSD BUY", date=NOW, edit_date=NOW + timedelta(minutes=1),
                        reply_to=SimpleNamespace(reply_to_msg_id=3))
    new = to_incoming(-100, m, edited=False)
    edit = to_incoming(-100, m, edited=True)
    assert new.date == NOW and edit.date == NOW + timedelta(minutes=1)
    assert edit.edited and edit.reply_to_msg_id == 3 and edit.chat_id == -100
