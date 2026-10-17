"""SQLite persistence. Every decision (executed / rejected + reason) ends up in `events`."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from signal_copier.models import FollowUp, IncomingMessage, Signal

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    edited INTEGER NOT NULL,
    text_hash TEXT NOT NULL,
    text TEXT NOT NULL,
    reply_to_msg_id INTEGER,
    date TEXT NOT NULL,
    received_at TEXT NOT NULL,
    parse_status TEXT,
    UNIQUE (chat_id, message_id, text_hash)
);
CREATE TABLE IF NOT EXISTS signals (
    key TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_type TEXT NOT NULL,
    entry_price TEXT,
    zone_low TEXT,
    zone_high TEXT,
    sl TEXT,
    tps TEXT NOT NULL,
    date TEXT NOT NULL,
    status TEXT NOT NULL,          -- RECORDED | REJECTED | EXECUTED | FAILED
    reason TEXT,
    raw_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS signals_msg ON signals (chat_id, message_id);
CREATE INDEX IF NOT EXISTS signals_symbol ON signals (chat_id, symbol, date);
CREATE TABLE IF NOT EXISTS follow_ups (
    key TEXT PRIMARY KEY,          -- chat:message:action[:symbol][>signal key, or >message]
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    signal_key TEXT,
    price TEXT,
    fraction TEXT,
    tp_index INTEGER,
    date TEXT NOT NULL,
    status TEXT NOT NULL,          -- RECORDED | EXECUTED | WAITING | REJECTED | FAILED
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    client_id TEXT PRIMARY KEY,    -- deterministic, sent to OANDA as clientExtensions.id
    signal_key TEXT NOT NULL,
    part INTEGER NOT NULL,
    instrument TEXT NOT NULL,
    units TEXT NOT NULL,
    type TEXT NOT NULL,
    price TEXT,
    sl TEXT,
    tp TEXT,
    status TEXT NOT NULL,          -- PENDING_SEND | SENT | FILLED | PENDING | CANCELLED | REJECTED | FAILED
    oanda_order_id TEXT,
    oanda_trade_id TEXT,
    response TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    signal_key TEXT NOT NULL,
    client_id TEXT,
    instrument TEXT NOT NULL,
    units TEXT NOT NULL,
    open_price TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    state TEXT NOT NULL,           -- OPEN | CLOSED
    close_price TEXT,
    realized_pl TEXT,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    decision TEXT,
    reason TEXT,
    signal_key TEXT,
    message_key TEXT,
    data TEXT
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _s(value: Any) -> str | None:
    return None if value is None else str(value)


def _default(o: Any):
    if isinstance(o, (Decimal, datetime)):
        return str(o)
    raise TypeError(type(o))


class Storage:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def _one(self, sql: str, *args) -> dict | None:
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, *args) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # -------------------------------------------------------------- messages

    def save_message(self, msg: IncomingMessage) -> bool:
        """False if this text is already the message's latest version, i.e. a re-delivery.

        An edit back to an earlier text (TP 4498.30 -> 4500.30 -> 4498.30) is a new version, not a re-delivery:
        that row becomes the latest again (one row per text), so the edit is handled.
        """
        text_hash = hashlib.sha256(msg.text.encode()).hexdigest()
        latest = self._one("SELECT text_hash FROM messages WHERE chat_id=? AND message_id=? ORDER BY id DESC LIMIT 1",
                           msg.chat_id, msg.message_id)
        if latest is not None and latest["text_hash"] == text_hash:
            return False
        self.conn.execute("DELETE FROM messages WHERE chat_id=? AND message_id=? AND text_hash=?",
                          (msg.chat_id, msg.message_id, text_hash))
        self.conn.execute(
            "INSERT INTO messages (chat_id, message_id, edited, text_hash, text, reply_to_msg_id, date, received_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (msg.chat_id, msg.message_id, int(msg.edited), text_hash, msg.text, msg.reply_to_msg_id,
             msg.date.isoformat(), _now()),
        )
        return True

    def set_message_status(self, msg: IncomingMessage, status: str) -> None:
        text_hash = hashlib.sha256(msg.text.encode()).hexdigest()
        self.conn.execute(
            "UPDATE messages SET parse_status=? WHERE chat_id=? AND message_id=? AND text_hash=?",
            (status, msg.chat_id, msg.message_id, text_hash),
        )

    def last_message_id(self, chat_id: int) -> int | None:
        row = self.conn.execute("SELECT MAX(message_id) FROM messages WHERE chat_id=?", (chat_id,)).fetchone()
        return row[0]

    def message_reply_to(self, chat_id: int, message_id: int) -> int | None:
        """The message this one replies to (None if not stored or not a reply)."""
        row = self._one("SELECT reply_to_msg_id FROM messages WHERE chat_id=? AND message_id=? ORDER BY id DESC"
                        " LIMIT 1", chat_id, message_id)
        return row["reply_to_msg_id"] if row else None

    def latest_message_text(self, chat_id: int, message_id: int) -> str | None:
        row = self._one("SELECT text FROM messages WHERE chat_id=? AND message_id=? ORDER BY id DESC LIMIT 1",
                        chat_id, message_id)
        return row["text"] if row else None

    def message_posted_at(self, chat_id: int, message_id: int) -> datetime | None:
        """When the message was first seen with its post date (edits never move it later)."""
        row = self._one("SELECT MIN(date) AS d FROM messages WHERE chat_id=? AND message_id=?", chat_id, message_id)
        return datetime.fromisoformat(row["d"]) if row and row["d"] else None

    # --------------------------------------------------------------- signals

    def get_signal(self, key: str) -> dict | None:
        return self._one("SELECT * FROM signals WHERE key=?", key)

    def signals_for_message(self, chat_id: int, message_id: int) -> list[dict]:
        return self._all("SELECT * FROM signals WHERE chat_id=? AND message_id=? ORDER BY idx", chat_id, message_id)

    def open_signals_for_symbol(self, chat_id: int, symbol: str) -> list[dict]:
        """Traded signals on this symbol that still have an open trade or a pending/unknown order."""
        return self._all(
            "SELECT * FROM signals WHERE chat_id=? AND symbol=? AND status IN ('EXECUTED','RECORDED') AND key IN"
            " (SELECT signal_key FROM trades WHERE state='OPEN'"
            "  UNION SELECT signal_key FROM orders WHERE status IN ('PENDING','SENT'))"
            " ORDER BY date DESC",
            chat_id, symbol,
        )

    def live_signal_message_ids(self, chat_id: int) -> list[int]:
        """Posts of traded signals that still have an open trade or a pending/unknown order."""
        rows = self.conn.execute(
            "SELECT DISTINCT message_id FROM signals WHERE chat_id=? AND status='EXECUTED' AND key IN"
            " (SELECT signal_key FROM trades WHERE state='OPEN'"
            "  UNION SELECT signal_key FROM orders WHERE status IN ('PENDING','SENT')) ORDER BY message_id",
            (chat_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def recent_signals_for_symbol(self, chat_id: int, symbol: str, since: datetime) -> list[dict]:
        return self._all(
            "SELECT * FROM signals WHERE chat_id=? AND symbol=? AND date>=? AND status IN ('EXECUTED','RECORDED')"
            " ORDER BY date DESC",
            chat_id, symbol, since.isoformat(),
        )

    def upsert_signal(self, s: Signal, status: str, reason: str | None = None) -> None:
        now = _now()
        self.conn.execute(
            """INSERT INTO signals (key, chat_id, message_id, idx, symbol, side, entry_type, entry_price, zone_low,
                   zone_high, sl, tps, date, status, reason, raw_text, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET symbol=excluded.symbol, side=excluded.side,
                   entry_type=excluded.entry_type, entry_price=excluded.entry_price, zone_low=excluded.zone_low,
                   zone_high=excluded.zone_high, sl=excluded.sl, tps=excluded.tps, status=excluded.status,
                   reason=excluded.reason, raw_text=excluded.raw_text, updated_at=excluded.updated_at""",
            (s.key, s.chat_id, s.message_id, s.index, s.symbol, s.side.value, s.entry.type.value,
             _s(s.entry.price), _s(s.entry.zone_low), _s(s.entry.zone_high), _s(s.sl),
             json.dumps([str(t) for t in s.tp]), s.date.isoformat(), status, reason, s.raw_text, now, now),
        )

    def set_signal_status(self, key: str, status: str, reason: str | None = None) -> None:
        self.conn.execute("UPDATE signals SET status=?, reason=?, updated_at=? WHERE key=?", (status, reason, _now(), key))

    def update_signal_levels(self, key: str, sl: Decimal | None, tps: list[Decimal]) -> None:
        self.conn.execute(
            "UPDATE signals SET sl=?, tps=?, updated_at=? WHERE key=?",
            (_s(sl), json.dumps([str(t) for t in tps]), _now(), key),
        )

    def update_signal_text(self, key: str, raw_text: str) -> None:
        """The post's latest handled version (an edit is compared with it, not with the first one)."""
        self.conn.execute("UPDATE signals SET raw_text=?, updated_at=? WHERE key=?", (raw_text, _now(), key))

    # ------------------------------------------------------------ follow-ups

    @staticmethod
    def follow_up_key(fu: FollowUp, legacy: bool = False) -> str:
        """One per message, action and symbol ("Close AUDUSD" + "Close XAUUSD" in one message are two).

        legacy: the key used before the symbol was part of it (rows written by older versions).
        """
        key = f"{fu.chat_id}:{fu.message_id}:{fu.action.value}"
        return key if legacy or fu.symbol is None else f"{key}:{fu.symbol}"

    def get_follow_up(self, key: str) -> dict | None:
        return self._one("SELECT * FROM follow_ups WHERE key=?", key)

    def follow_ups_for(self, chat_id: int, message_id: int, action: str) -> list[dict]:
        """Every row of this message and action, whatever symbol its key names."""
        return self._all("SELECT * FROM follow_ups WHERE chat_id=? AND message_id=? AND action=? ORDER BY created_at",
                         chat_id, message_id, action)

    def save_follow_up(self, fu: FollowUp, signal_key: str | None, status: str, reason: str | None = None,
                       key: str | None = None) -> None:
        """key: the row, when not follow_up_key(fu) (two commands of one message with the same key)."""
        self.conn.execute(
            """INSERT INTO follow_ups (key, chat_id, message_id, action, signal_key, price, fraction, tp_index, date,
                   status, reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET signal_key=excluded.signal_key, status=excluded.status,
                   reason=excluded.reason, price=excluded.price, fraction=excluded.fraction""",
            (key or self.follow_up_key(fu), fu.chat_id, fu.message_id, fu.action.value, signal_key, _s(fu.price),
             _s(fu.fraction), fu.tp_index, fu.date.isoformat(), status, reason, _now()),
        )

    def update_follow_up(self, key: str, status: str, reason: str | None = None) -> None:
        self.conn.execute("UPDATE follow_ups SET status=?, reason=? WHERE key=?", (status, reason, key))

    def follow_ups_with_status(self, status: str) -> list[dict]:
        return self._all("SELECT * FROM follow_ups WHERE status=? ORDER BY created_at", status)

    # ---------------------------------------------------------------- orders

    def create_order(self, client_id: str, signal_key: str, part: int, instrument: str, units: Decimal,
                     type_: str, price: Decimal | None, sl: Decimal | None, tp: Decimal | None) -> bool:
        """False if the order already exists (never send it twice)."""
        now = _now()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO orders (client_id, signal_key, part, instrument, units, type, price, sl, tp, status,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,'PENDING_SEND',?,?)",
            (client_id, signal_key, part, instrument, str(units), type_, _s(price), _s(sl), _s(tp), now, now),
        )
        return cur.rowcount == 1

    def get_order(self, client_id: str) -> dict | None:
        return self._one("SELECT * FROM orders WHERE client_id=?", client_id)

    def update_order(self, client_id: str, **fields: Any) -> None:
        if "response" in fields and not isinstance(fields["response"], str):
            fields["response"] = json.dumps(fields["response"], default=_default)
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE orders SET {cols} WHERE client_id=?", (*fields.values(), client_id))

    def orders_with_status(self, *statuses: str) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        return self._all(f"SELECT * FROM orders WHERE status IN ({marks})", *statuses)

    def orders_expired_unknown(self, since: str) -> list[dict]:
        """Orders without answer that were given up as never executed (FAILED by the monitor) since `since`."""
        return self._all("SELECT * FROM orders WHERE status='FAILED' AND response LIKE '%\"expired_unknown\"%'"
                         " AND updated_at>=?", since)

    def orders_for_signal(self, signal_key: str) -> list[dict]:
        return self._all("SELECT * FROM orders WHERE signal_key=? ORDER BY part", signal_key)

    def known_tickets(self) -> set[str]:
        """Every broker ticket the bot has a row for (trades, pending orders, filled orders)."""
        rows = self.conn.execute(
            "SELECT trade_id FROM trades UNION SELECT oanda_order_id FROM orders WHERE oanda_order_id IS NOT NULL"
            " UNION SELECT oanda_trade_id FROM orders WHERE oanda_trade_id IS NOT NULL"
        ).fetchall()
        return {str(r[0]) for r in rows}

    # ---------------------------------------------------------------- trades

    def upsert_trade(self, trade_id: str, signal_key: str, client_id: str | None, instrument: str, units: Decimal,
                     open_price: Decimal, opened_at: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO trades (trade_id, signal_key, client_id, instrument, units, open_price, opened_at,"
            " state) VALUES (?,?,?,?,?,?,?,'OPEN')",
            (trade_id, signal_key, client_id, instrument, str(units), str(open_price), opened_at),
        )

    def open_trades(self) -> list[dict]:
        return self._all("SELECT * FROM trades WHERE state='OPEN'")

    def trades_for_signal(self, signal_key: str, open_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM trades WHERE signal_key=?" + (" AND state='OPEN'" if open_only else "")
        return self._all(sql, signal_key)

    def close_trade(self, trade_id: str, close_price: Decimal | None, realized_pl: Decimal | None,
                    closed_at: str | None) -> None:
        self.conn.execute(
            "UPDATE trades SET state='CLOSED', close_price=?, realized_pl=?, closed_at=? WHERE trade_id=?",
            (_s(close_price), _s(realized_pl), closed_at or _now(), trade_id),
        )

    # ---------------------------------------------------------------- events

    def add_event(self, kind: str, decision: str | None = None, reason: str | None = None,
                  signal_key: str | None = None, message_key: str | None = None, data: Any = None) -> None:
        self.conn.execute(
            "INSERT INTO events (ts, kind, decision, reason, signal_key, message_key, data) VALUES (?,?,?,?,?,?,?)",
            (_now(), kind, decision, reason, signal_key, message_key,
             None if data is None else json.dumps(data, default=_default, ensure_ascii=False)),
        )

    def events(self, kind: str | None = None) -> list[dict]:
        if kind:
            return self._all("SELECT * FROM events WHERE kind=? ORDER BY id", kind)
        return self._all("SELECT * FROM events ORDER BY id")

    # ----------------------------------------------------------------- state

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self._one("SELECT value FROM state WHERE key=?", key)
        return row["value"] if row else default

    def set_state(self, key: str, value: str | None) -> None:
        if value is None:
            self.conn.execute("DELETE FROM state WHERE key=?", (key,))
        else:
            self.conn.execute(
                "INSERT INTO state (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def open_positions(self, include_pending: bool = True) -> list[dict]:
        """One row per signal that still has an open trade or a pending order.

        include_pending=False leaves out confirmed pending LIMIT/STOP orders; an order in unknown state (SENT)
        still counts, it may be an open position.
        """
        orders = "('PENDING','SENT')" if include_pending else "('SENT')"
        return self._all(
            "SELECT signal_key, instrument FROM trades WHERE state='OPEN'"
            f" UNION SELECT signal_key, instrument FROM orders WHERE status IN {orders}"
        )

    def signal_counts_since(self, since: datetime) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) FROM signals WHERE created_at>=? GROUP BY status", (since.isoformat(),)
        ).fetchall()
        return {r[0]: r[1] for r in rows}
