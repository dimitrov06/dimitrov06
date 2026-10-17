"""Final review of today's changes: sizing and order tracking against the fake MT5 (990 EUR, 3% risk).

- a tick value of 0 is never sized from the commission alone (zero-tick-value-commission-sizing);
- an order without answer is not given up while MT5 has a bot position on its symbol that no row knows
  (unknown-expiry-ignores-untracked-bot-positions);
- a monitor round never interleaves with an order send (unlocked-sync-race-reports-open-trade-as-closed).
"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace as NS

from conftest import NOW
from fake_mt5 import StrictMt5
from owner_setup import AUDUSD_SIGNAL, CHAT, XAUUSD_MARKET, bot_positions, events, make, post, run, sig
from signal_copier.listener.telegram import to_incoming


# ------------------------------------------------- zero-tick-value-commission-sizing

def _no_tick_value(fake):
    d, vmin, step, _, ts, fill = fake.symbols["XAUUSD"]
    fake.symbols["XAUUSD"] = (d, vmin, step, 0.0, ts, fill)  # symbol just selected / rate not synced yet


def test_zero_tick_value_with_a_commission_is_rejected_not_sized_from_the_commission():
    fake = StrictMt5(demo=False)
    fake.balance = fake.equity = 990.0
    _no_tick_value(fake)
    p = make(fake, commission_per_lot="5.5")
    post(p, XAUUSD_MARKET, 505)
    assert fake.market_sends() == []
    assert events(p, "risk") == [("REJECTED", "no_tick_value")]
    assert any(t.startswith("🚫") and "no_tick_value" in t for t in p.notifier.sent)


def test_zero_tick_value_without_a_commission_is_rejected_too():
    fake = StrictMt5(demo=False)
    _no_tick_value(fake)
    p = make(fake)
    post(p, XAUUSD_MARKET, 505)
    assert fake.market_sends() == []
    assert sig(p, 505)["reason"] == "no_tick_value"


def test_normal_tick_value_with_a_commission_still_trades():
    fake = StrictMt5(demo=False)
    fake.balance = fake.equity = 990.0
    p = make(fake, commission_per_lot="5.5")
    post(p, XAUUSD_MARKET, 505)
    (send,) = fake.market_sends()
    assert 0 < send["volume"] <= 0.03


# ------------------------------------------------- unknown-expiry-ignores-untracked-bot-positions

class TruncatingMt5(StrictMt5):
    """The broker keeps only the first 10 characters of an order comment."""

    def _execute(self, req):
        if "comment" in req and "position" not in req:
            req = {**req, "comment": req["comment"][:10]}
        return super()._execute(req)


def _lost_answer_with_retagged_position(**risk):
    fake = TruncatingMt5(demo=False)
    fake.script = ["lost"]  # executes, the answer is lost
    p = make(fake, **risk)
    clock = {"t": NOW}
    p.executor.clock = lambda: clock["t"]
    post(p, XAUUSD_MARKET, 505, NOW)
    for m in (1, 6, 11, 16):
        clock["t"] = NOW + timedelta(minutes=m)
        run(p.monitor_once())
    return fake, p


def test_order_without_answer_is_not_given_up_while_an_untracked_bot_position_is_live():
    fake, p = _lost_answer_with_retagged_position(max_open_positions=1)
    assert len(bot_positions(fake)) == 1
    assert [o["status"] for o in p.storage.orders_for_signal(f"{CHAT}:505:0")] == ["SENT"]  # keeps its slot
    assert not any(t.startswith("⌛") for t in p.notifier.sent)
    stray = [t for t in p.notifier.sent if "която ботът не следи" in t]
    assert len(stray) == 1 and "#1000" in stray[0]  # told once, not every round
    post(p, AUDUSD_SIGNAL, 506, NOW + timedelta(minutes=17))
    assert sig(p, 506)["status"] == "REJECTED" and len(bot_positions(fake)) == 1  # no second trade


def test_order_without_answer_still_expires_once_the_stray_position_is_gone():
    fake, p = _lost_answer_with_retagged_position()
    (stray,) = bot_positions(fake)
    fake.hit_tp(stray.ticket, 4498.30)  # the owner closes it in MT5 (or it ends)
    clock = {"t": NOW + timedelta(minutes=17)}
    p.executor.clock = lambda: clock["t"]
    run(p.monitor_once())
    assert [o["status"] for o in p.storage.orders_for_signal(f"{CHAT}:505:0")] == ["FAILED"]
    assert any(t.startswith("⌛") for t in p.notifier.sent)


def test_untracked_position_on_another_symbol_does_not_block_the_expiry():
    fake = StrictMt5(demo=False)
    fake.positions[777] = NS(ticket=777, identifier=777, symbol="AUDUSD", type=0, volume=0.1, price_open=0.694,
                             sl=0.69, tp=0.70, magic=770077, comment="sc-old")  # an AUDUSD the bot lost track of
    fake.script = ["none"]  # not executed, no answer
    p = make(fake)
    clock = {"t": NOW}
    p.executor.clock = lambda: clock["t"]
    post(p, XAUUSD_MARKET, 505, NOW)
    for m in (1, 6, 11):
        clock["t"] = NOW + timedelta(minutes=m)
        run(p.monitor_once())
    assert [o["status"] for o in p.storage.orders_for_signal(f"{CHAT}:505:0")] == ["FAILED"]


# ------------------------------------------------- unlocked-sync-race-reports-open-trade-as-closed

def test_monitor_round_during_a_send_does_not_report_the_new_position_as_closed():
    fake = StrictMt5(demo=False)
    p = make(fake)
    client = p.client
    orig = client.call
    state = {"task": None}

    async def call(fn, *a, **kw):
        if fn == "order_send" and state["task"] is None:
            state["task"] = asyncio.create_task(p.monitor_once())  # the 30 s monitor tick lands here
            await asyncio.sleep(0)
        return await orig(fn, *a, **kw)

    client.call = call

    async def main():
        p.test_clock["t"] = NOW + timedelta(seconds=2)
        tm = NS(id=505, raw_text=XAUUSD_MARKET, date=NOW, edit_date=None, reply_to=None)
        await p.handle_message(to_incoming(CHAT, tm, edited=False))
        await state["task"]

    asyncio.run(main())
    assert len(bot_positions(fake)) == 1 and len(p.storage.open_trades()) == 1
    assert not any("вече е затворена" in t or "Поръчката без отговор" in t for t in p.notifier.sent)
