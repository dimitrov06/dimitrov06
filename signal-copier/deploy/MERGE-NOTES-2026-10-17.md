# Merging the 2026-10-17 audit fixes into the copy on the owner's PC

Written for whoever merges this branch into `C:\Users\Ivilin\trading-bot\signal-copier`.
That copy has local fixes that never reached GitHub, and this branch has fixes the PC
copy does not have. Both sets must survive the merge.

## Safety first

1. Make a full copy of the PC folder before touching it (code, `config.yaml`, `.env`, `data\`).
   `data\signals.db` is the live database. The schema did not change (no new tables or
   columns), so the existing file works with the new code.
2. Do not stop or restart the bot yourself. When the merge is done and all tests pass, the
   owner restarts it: Task Scheduler, task SignalCopier, End, then Run.
3. Never commit or print `.env`, tokens or passwords.

## What is on this branch since 800606d

Run `git log --oneline 800606d..origin/claude/signal-copier-6wbiqf`. In short:

- **Executor (13ed244, 405adba).** A `None` read from MT5 is an error, never "empty".
  After a lost answer (order_send `None`, TIMEOUT, no connection) the order is **never
  sent again**: the tag is polled for about 15 s, then the row stays SENT. `sync()` adopts
  it later, or it expires after `executor.unknown_order_expiry_minutes` (10) once every
  read answered and nothing has the tag. Other parts:
  - Cancel never marks a filled or unreachable order CANCELLED.
  - Follow-ups first adopt fills that happened since the last sync.
  - The deal history window allows for server time.
  - `connect()` refuses live when Algo Trading is off or the account is not hedging.
- **Pipeline and risk (31c430a).**
  - Edits never revive a rejected or old signal and never loosen the SL.
  - Follow-up targets: replies are followed through up to 3 hops. A symbol-only command
    targets only live trades, and two live trades of one symbol is ambiguous.
  - BE that MT5 cannot accept yet waits (WAITING).
  - Sizing uses 3% of min(balance, equity). `cap_to_daily_budget`, the late-entry floor
    (`late_entry_min_sl_fraction`) and `max_lots` are new.
  - The daily stop cancels the bot's pending orders.
  - On start, messages missed while the bot was down are caught up.
  - Notifications go out from a background queue.
  - `run` restarts itself after a crash.
  - **Owner decision:** `risk.count_pending_in_max_open: false` (default). Pending orders do
    not take one of the 3 slots. Per symbol they still count.
- **Parser (a5f543e).**
  - Strict command forms; negated, modal and conditional commands are UNCERTAIN.
  - Descriptive "close"/"exit" wording is ignored.
  - Partial-close wording gives a partial close.
  - Updates and reposts are never new signals.
  - Unknown order types are never taken at market.
- **E2E (993e870).** `tests/test_e2e_mt5.py` runs the owner's scenarios against the
  faithful fake. Two fixes came with it: fills and closes between monitor rounds are no
  longer lost, and commands for an order with no answer yet wait.
- **Final review (34ef811, 93544c7 and any later commits).**
  - Retried commands keep their original target.
  - A Telegram disconnect raises, so the process restarts instead of running deaf.
  - When MT5 is down, the owner is told, and after about 5 min the process restarts.
  - Posts of live signals are re-read on start, so edits made while the bot was down are
    applied.
  - A zero tick value rejects the trade.
  - Expiry waits while an untracked bot position exists.
  - Commands written into a signal post are retried after a failure and compared with
    their price, so a corrected "Move SL to X" is applied.
  - "Close X now, 50%" or "..., half" is UNCERTAIN instead of a full close.
  - A broken custom rules file is reported once, not every 30 s.

## Topics only the PC copy has: keep them, and combine them like this

| PC-only fix | What this branch has | How to combine |
|---|---|---|
| MT5 IPC stale: reconnect + retry of order_send (2026-10-16) | never resend after a lost answer | **Critical.** Keep the reconnect. A retry may resend only when the order provably never reached the server: the failure came before `order_send`, or the result had a definite reject retcode. After `None`, TIMEOUT or no connection, use this branch's `_await_tag` path and never send again. Otherwise one signal can open two positions (6% risk). |
| Pending expiry in server time, 14 days | `pending_order_expiry_minutes` (240) with a UTC epoch | Keep the PC version. |
| Stale PENDING rows for orders that expired or were cancelled at the broker | `sync()` adopts fills via `_opening_deal`; SENT rows expire | Keep both. Only ever free a row after reads that answered (never on `None`). |
| Edits applied to MT5 pending orders | `amend_levels` sends only changed levels and never loosens the SL, for positions | Apply the same two rules to pending orders. |
| Margin-aware lot reduction | new sizing limits (min(balance, equity), daily budget, late-entry floor, `max_lots`, `no_tick_value`) | Reduce for margin **after** this branch's sizing, never above it. |
| Crossed limit/stop sent at market | market checks: bid/ask beyond SL, `price_moved_too_far`, 1.1× SL-distance guard on each send | A converted order must pass the same market checks. |
| 15 s channel poll fallback, Telegram reconnect | catch-up on start, live-post re-check, ConnectionError then restart | Keep the poll. Make sure a polled message and a live event are not both handled: dedup is `Storage.save_message` (latest text per message). |
| Parser from the full channel history (USTech/German40, "Update the SL to the entry price", "very close to", reports ignored, "Bu now", 20% SL limit) | new safety sections in `rules.yaml` (not_commands, command_guards, unclear, results, update_words, ...) | Keep every PC rule and PC real-sample test. Run them all with the new sections. A PC sample that now comes out UNCERTAIN must be looked at, not silently accepted. |
| Command without reply finds the signal up to 14 days back | symbol-only command targets live trades however old; two live = ambiguous | Use this branch's logic. It already covers the PC case and refuses to guess. |
| `/help` with every command | same text (800606d) | No conflict. |

## After the merge

1. Run both test suites, this branch's tests and the PC's own tests: `python -m pytest -q`.
   All must pass.
2. Run `python -m signal_copier check` against the real terminal. It must show the account,
   that Algo Trading is on and that the account is hedging, and it must send no orders.
3. Tell the owner to restart the bot. After the restart, check:
   - `data\bot.log` for the start lines and the "👂 Следя ..." notice;
   - `/status` in @TradingCIBot.
4. If the PC copy can push, push the merged code to this branch so GitHub and the PC are the
   same again.

## Settings the owner may want (all optional)

- `risk.commission_per_lot`: round-turn commission per lot in EUR. Set it only on a Raw
  account (about 5.5). On a Standard account leave it at 0.
- `risk.max_lots`, for example `{XAU_USD: 0.5}`: a hard cap against a typo SL next to the
  entry.
